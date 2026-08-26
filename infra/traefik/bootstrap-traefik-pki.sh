#!/bin/sh
set -eu

PKI_DIR="${1:-/pki}"
ROOT_CA_KEY="$PKI_DIR/rootCA.key"
ROOT_CA_CRT="$PKI_DIR/rootCA.crt"
SERVER_KEY="$PKI_DIR/dass.localhost.key"
SERVER_CRT="$PKI_DIR/dass.localhost.crt"
SERVER_CSR="$PKI_DIR/dass.localhost.csr"
SERVER_EXT="$PKI_DIR/dass.localhost.ext"
ROOT_CA_SERIAL="$PKI_DIR/rootCA.srl"
ROOT_CA_EXT="$PKI_DIR/rootCA.ext"

mkdir -p "$PKI_DIR"

if [ ! -f "$ROOT_CA_KEY" ] || [ ! -f "$ROOT_CA_CRT" ]; then
  # The CA extensions are spelled out on purpose. Without an explicit keyUsage,
  # OpenSSL 3.4 and later refuse the certificate outright
  # ("CA certificate does not include key usage extension"), which breaks every
  # client on a current TLS stack while older ones still accept it.
  # A self-contained config: this runs under busybox sh, which has no process
  # substitution, and it avoids depending on the image's openssl.cnf layout.
  cat >"$ROOT_CA_EXT" <<'EOF'
[req]
distinguished_name = dn
x509_extensions = v3_dass_ca
prompt = no

[dn]
CN = DASS Local Root CA

[v3_dass_ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
EOF
  openssl genrsa -out "$ROOT_CA_KEY" 4096 >/dev/null 2>&1
  openssl req -x509 -new -nodes \
    -key "$ROOT_CA_KEY" \
    -sha256 \
    -days 3650 \
    -out "$ROOT_CA_CRT" \
    -config "$ROOT_CA_EXT" >/dev/null 2>&1
fi

if [ ! -f "$SERVER_KEY" ] || [ ! -f "$SERVER_CRT" ]; then
  openssl genrsa -out "$SERVER_KEY" 2048 >/dev/null 2>&1
  cat >"$SERVER_EXT" <<'EOF'
subjectAltName=DNS:dass.localhost,DNS:localhost,IP:127.0.0.1,IP:::1
extendedKeyUsage=serverAuth
keyUsage=critical,digitalSignature,keyEncipherment
basicConstraints=critical,CA:FALSE
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF
  openssl req -new \
    -key "$SERVER_KEY" \
    -out "$SERVER_CSR" \
    -subj "/CN=dass.localhost" >/dev/null 2>&1
  openssl x509 -req \
    -in "$SERVER_CSR" \
    -CA "$ROOT_CA_CRT" \
    -CAkey "$ROOT_CA_KEY" \
    -CAcreateserial \
    -out "$SERVER_CRT" \
    -days 825 \
    -sha256 \
    -extfile "$SERVER_EXT" >/dev/null 2>&1
fi

chmod 600 "$ROOT_CA_KEY" "$SERVER_KEY"
rm -f "$SERVER_CSR" "$SERVER_EXT" "$ROOT_CA_EXT" "$ROOT_CA_SERIAL"

printf '%s\n' "$ROOT_CA_CRT"
