#!/bin/sh
# Regenerate the test certificates in tests/support/pki/. They are committed
# so that the tests don't need openssl, and are valid for 100 years.
#
#   ca.pem                       test CA (its key is thrown away)
#   server.pem, server.key       server certificate for "localhost", signed by the CA
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

DAYS=36500
openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.pem -days $DAYS \
    -subj "/CN=rrcc test CA" 2>/dev/null

openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr -subj "/CN=localhost" 2>/dev/null
printf 'subjectAltName=DNS:localhost\n' > server.ext
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial -out server.pem \
    -days $DAYS -extfile server.ext 2>/dev/null

openssl req -newkey rsa:2048 -nodes -keyout client.key -out client.csr -subj "/CN=rrcc test client" 2>/dev/null
openssl x509 -req -in client.csr -CA ca.pem -CAkey ca.key -CAcreateserial -out client.pem \
    -days $DAYS 2>/dev/null
cat client.pem client.key > client-combined.pem
openssl rsa -in client.key -aes256 -passout pass:secret -out client-encrypted.key 2>/dev/null

openssl req -x509 -newkey rsa:2048 -nodes -keyout other-ca.key -out other-ca.pem -days $DAYS \
    -subj "/CN=rrcc other CA" 2>/dev/null

mkdir cadir
cp ca.pem "cadir/$(openssl x509 -hash -noout -in ca.pem).0"

rm -f server.csr server.ext client.csr ca.srl ca.key other-ca.key
