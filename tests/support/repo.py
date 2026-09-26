"""Build small RPM repositories on disk for the tests.

The packages are not real RPMs, just files with some content: rrcc only
looks at their presence, size and checksum. The metadata (repomd.xml,
primary, filelists, other, updateinfo, modules.yaml) is complete enough for
rrcc and dnf.

    repo = RepoBuilder(path)
    repo.add("foo", "1.0", "1")
    repo.add("foo", "1.1", "1")
    repo.build()
    os.remove(repo.package_path("foo", "1.0"))
"""

import gzip
import hashlib
import lzma
import os
import shutil
import subprocess
from xml.sax.saxutils import escape, quoteattr

from . import have_command


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def compress(data, fmt, workdir):
    """Compress data with gz, xz, zst, zck or none (for plain files)."""
    if fmt == "gz":
        return gzip.compress(data)
    if fmt == "xz":
        return lzma.compress(data)
    if fmt == "zst":
        try:
            from compression import zstd
            return zstd.compress(data)
        except ImportError:
            import zstandard
            return zstandard.ZstdCompressor().compress(data)
    if fmt == "zck":
        if not have_command("zck"):
            raise RuntimeError("the 'zck' command is needed for .zck metadata")
        src = os.path.join(workdir, "zck-input")
        with open(src, "wb") as f:
            f.write(data)
        subprocess.run(["zck", "-o", src + ".zck", src], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(src + ".zck", "rb") as f:
            out = f.read()
        os.remove(src)
        os.remove(src + ".zck")
        return out
    if fmt == "none":
        return data
    raise ValueError(fmt)


class RepoBuilder:
    def __init__(self, root):
        self.root = root
        self.packages = []
        self.modules = []
        self.metadata = {}  # type -> path of the file written by build()

    # -- content ----------------------------------------------------------

    def add(self, name, version="1.0", release="1", arch="x86_64", epoch="0",
            href=None, content=None, write=True, checksum_type="sha256"):
        """Add a package. It is written to disk unless write is False (for
        a package that the metadata lists but the mirror lacks). Returns
        the package as a dict."""
        if href is None:
            href = f"Packages/{name[0]}/{name}-{version}-{release}.{arch}.rpm"
        if content is None:
            # distinct content, and sizes that differ between packages
            content = f"{name}-{epoch}:{version}-{release}.{arch}\n".encode() * (len(self.packages) + 3)
        pkg = {"name": name, "version": version, "release": release, "arch": arch,
               "epoch": epoch, "href": href, "content": content, "write": write,
               "checksum_type": checksum_type}
        self.packages.append(pkg)
        return pkg

    def add_module(self, name, stream, version, artifacts, context="c0", arch="x86_64"):
        """Add a module build. artifacts are NEVRAs, name-epoch:version-release.arch."""
        self.modules.append({"name": name, "stream": stream, "version": version,
                             "context": context, "arch": arch, "artifacts": list(artifacts)})

    @staticmethod
    def nevra(pkg):
        return f"{pkg['name']}-{pkg['epoch']}:{pkg['version']}-{pkg['release']}.{pkg['arch']}"

    # -- writing ----------------------------------------------------------

    def _primary_xml(self, packages):
        out = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<metadata xmlns="http://linux.duke.edu/metadata/common" '
               f'xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="{len(packages)}">']
        for p in packages:
            csum = self._pkg_checksum(p)
            out.append(
                f'<package type="rpm"><name>{escape(p["name"])}</name><arch>{p["arch"]}</arch>'
                f'<version epoch="{p["epoch"]}" ver={quoteattr(p["version"])} rel={quoteattr(p["release"])}/>'
                f'<checksum type="{p["checksum_type"]}" pkgid="YES">{csum}</checksum>'
                f'<summary>test</summary><description>test</description><packager/><url/>'
                f'<time file="1" build="1"/>'
                f'<size package="{len(p["content"])}" installed="1" archive="1"/>'
                f'<location href={quoteattr(p["href"])}/>'
                f'<format><rpm:license>MIT</rpm:license></format></package>')
        out.append("</metadata>")
        return ("\n".join(out) + "\n").encode()

    @staticmethod
    def _pkg_checksum(p):
        algo = p["checksum_type"]
        if algo in hashlib.algorithms_available and algo != "sha":
            return hashlib.new(algo, p["content"]).hexdigest()
        return sha256(p["content"])  # unknown algo: any value will do

    def _pkgid_xml(self, kind):
        ns = {"filelists": "filelists", "other": "other"}[kind]
        out = ['<?xml version="1.0" encoding="UTF-8"?>',
               f'<{kind} xmlns="http://linux.duke.edu/metadata/{ns}" packages="{len(self.packages)}">']
        for p in self.packages:
            out.append(f'<package pkgid="{self._pkg_checksum(p)}" name={quoteattr(p["name"])} '
                       f'arch="{p["arch"]}"><version epoch="{p["epoch"]}" '
                       f'ver={quoteattr(p["version"])} rel={quoteattr(p["release"])}/></package>')
        out.append(f"</{kind}>")
        return ("\n".join(out) + "\n").encode()

    def modules_yaml(self):
        """modules.yaml in the layout libmodulemd writes."""
        out = []
        for m in self.modules:
            out += ["---", "document: modulemd", "version: 2", "data:",
                    f"  name: {m['name']}", f'  stream: "{m["stream"]}"',
                    f"  version: {m['version']}", f"  context: {m['context']}",
                    f"  arch: {m['arch']}", "  summary: Test module",
                    "  description: >-", "    Test module.",
                    "  license:", "    module:", "    - MIT"]
            if m["artifacts"]:
                out += ["  artifacts:", "    rpms:"] + [f"    - {a}" for a in m["artifacts"]]
            out.append("...")
        return ("\n".join(out) + "\n").encode()

    def build(self, primary="gz", primary_zck=False, zck_packages=None,
              updateinfo=True, drop_sizes=False):
        """Write the packages and repodata/. primary is the compression of
        primary.xml. With primary_zck, a primary_zck entry is added too,
        listing zck_packages (default: the same packages as primary)."""
        repodata = os.path.join(self.root, "repodata")
        shutil.rmtree(repodata, ignore_errors=True)
        os.makedirs(repodata)
        for p in self.packages:
            if p["write"]:
                path = os.path.join(self.root, p["href"])
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(p["content"])

        entries = []

        def add(data_type, basename, raw, fmt):
            data = compress(raw, fmt, repodata)
            csum = sha256(data)
            suffix = "" if fmt == "none" else "." + fmt
            href = f"repodata/{csum}-{basename}{suffix}"
            path = os.path.join(self.root, href)
            with open(path, "wb") as f:
                f.write(data)
            self.metadata[data_type] = path
            size = "" if drop_sizes else f"<size>{len(data)}</size><open-size>{len(raw)}</open-size>"
            entries.append(
                f'  <data type="{data_type}">\n'
                f'    <checksum type="sha256">{csum}</checksum>\n'
                f'    <open-checksum type="sha256">{sha256(raw)}</open-checksum>\n'
                f'    <location href="{href}"/>\n'
                f'    <timestamp>1700000000</timestamp>\n'
                f'    {size}\n'
                f'  </data>')

        add("primary", "primary.xml", self._primary_xml(self.packages), primary)
        add("filelists", "filelists.xml", self._pkgid_xml("filelists"), "gz")
        add("other", "other.xml", self._pkgid_xml("other"), "gz")
        if self.modules:
            add("modules", "modules.yaml", self.modules_yaml(), "gz")
        if updateinfo:
            add("updateinfo", "updateinfo.xml",
                b'<?xml version="1.0" encoding="UTF-8"?>\n<updates/>\n', "xz")
        if primary_zck:
            pkgs = self.packages if zck_packages is None else zck_packages
            add("primary_zck", "primary.xml", self._primary_xml(pkgs), "zck")

        with open(os.path.join(repodata, "repomd.xml"), "w") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<repomd xmlns="http://linux.duke.edu/metadata/repo" '
                    'xmlns:rpm="http://linux.duke.edu/metadata/rpm">\n'
                    '  <revision>1700000000</revision>\n'
                    + "\n".join(entries) + "\n</repomd>\n")
        return self

    # -- inspecting and breaking a built repo ------------------------------

    def package_path(self, name, version=None):
        """Path of the (first) package with this name and version."""
        for p in self.packages:
            if p["name"] == name and (version is None or p["version"] == version):
                return os.path.join(self.root, p["href"])
        raise KeyError((name, version))

    @property
    def repomd(self):
        return os.path.join(self.root, "repodata", "repomd.xml")

    def edit_repomd(self, old, new):
        with open(self.repomd) as f:
            text = f.read()
        if old not in text:
            raise ValueError(f"{old!r} not in repomd.xml")
        with open(self.repomd, "w") as f:
            f.write(text.replace(old, new, 1))


def flip_byte(path):
    """Corrupt a file without changing its size."""
    with open(path, "r+b") as f:
        data = bytearray(f.read())
        data[len(data) // 2] ^= 0x01
        f.seek(0)
        f.write(data)


def truncate(path, nbytes=10):
    """Remove nbytes from the end of a file."""
    size = os.path.getsize(path)
    with open(path, "r+b") as f:
        f.truncate(size - nbytes)


def standard_repo(root, **build_args):
    """A repo with a few versions of some packages, including characters
    that need URL quoting (+ and ^) in file names."""
    repo = RepoBuilder(root)
    repo.add("alpha", "1.0", "1")
    repo.add("alpha", "1.1", "1")
    repo.add("alpha", "1.1", "1", arch="noarch")
    repo.add("beta", "2.0", "3.module_el8+1234+abcd")
    repo.add("gamma", "0.9^git20260101", "1")
    repo.add("delta", "5", "1", epoch="1")
    repo.build(**build_args)
    return repo
