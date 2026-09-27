#!/bin/sh
# Regenerate the test certificates in tests/support/pki/. They are committed
# so that the tests don't need openssl, and are valid for 100 years.
#
#   ca.pem                       test CA (its key is thrown away)
#   server.pem, server.key       server certificate for "localhost", signed by the CA
#   server-lax.pem, server-lax.key
#                                the same, but without key identifiers, which the
#                                strict X.509 checks reject (--no-strict-x509)
#   client.pem, client.key       client certificate, signed by the CA
#   client-combined.pem          client certificate and key in one file
#   client-encrypted.key         client key encrypted with the password "secret"
#   other-ca.pem                 an unrelated CA, which must not be trusted
#   cadir/                       ca.pem hashed for use as a CA directory

set -e
cd "$(dirname "$0")"
rm -rf pki
mkdir pki
cd pki

# The certificates must satisfy the strict X.509 checks that Python 3.13+
# enables by default (VERIFY_X509_STRICT): key identifiers everywhere and
# critical basic constraints on the CAs. The [lax] certificate deliberately
# lacks the key identifiers, like many certificates from home-made CAs.
cat > ext.cnf <<'EOT'
[req]
distinguished_name = dn
[dn]
[ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
[server]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:localhost
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
[lax]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:localhost
subjectKeyIdentifier = none
authorityKeyIdentifier = none
[client]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
EOT

DAYS=36500
ca() {  # ca NAME SUBJECT
    openssl req -x509 -newkey rsa:2048 -nodes -keyout "$1.key" -out "$1.pem" -days $DAYS \
        -subj "$2" -config ext.cnf -extensions ca 2>/dev/null
}
signed() {  # signed NAME SUBJECT EXTENSIONS
    openssl req -newkey rsa:2048 -nodes -keyout "$1.key" -out "$1.csr" -subj "$2" \
        -config ext.cnf 2>/dev/null
    openssl x509 -req -in "$1.csr" -CA ca.pem -CAkey ca.key -CAcreateserial -out "$1.pem" \
        -days $DAYS -extfile ext.cnf -extensions "$3" 2>/dev/null
}

ca ca "/CN=rrcc test CA"
signed server "/CN=localhost" server
signed server-lax "/CN=localhost" lax
signed client "/CN=rrcc test client" client
cat client.pem client.key > client-combined.pem
openssl rsa -in client.key -aes256 -passout pass:secret -out client-encrypted.key 2>/dev/null
ca other-ca "/CN=rrcc other CA"

mkdir cadir
cp ca.pem "cadir/$(openssl x509 -hash -noout -in ca.pem).0"

rm -f ext.cnf server.csr server-lax.csr client.csr ca.srl ca.key other-ca.key
