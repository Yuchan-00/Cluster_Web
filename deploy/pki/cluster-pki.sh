#!/bin/sh
# Internal CA for the agent channel (docs/design/security.md 8.1). Run on the admin PC:
# the CA private key must never be copied to the cluster.
#
#   cluster-pki.sh init-ca <dir>                     create ca.key (encrypted unless
#                                                    CLUSTER_PKI_NO_PASSPHRASE=1) and ca.pem
#   cluster-pki.sh issue <dir> <name> [san ...]      issue <name>.key/<name>.pem signed by the CA
#                                                    san: DNS:x or IP:y (default DNS:<name>)
#
# Certificates carry SubjectKeyIdentifier/AuthorityKeyIdentifier and key usages so that
# strict verifiers (Python 3.13 VERIFY_X509_STRICT) accept them.
set -eu

die() { echo "cluster-pki: $*" >&2; exit 1; }
[ $# -ge 2 ] || die "usage: $0 init-ca <dir> | issue <dir> <name> [san ...]"
command -v openssl >/dev/null || die "openssl not found"
cmd=$1; dir=$2; shift 2
umask 077

case $cmd in
init-ca)
    [ ! -e "$dir/ca.key" ] || die "$dir/ca.key already exists"
    mkdir -p "$dir"
    if [ "${CLUSTER_PKI_NO_PASSPHRASE:-0}" = 1 ]; then
        openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$dir/ca.key"
    else
        openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -aes-256-cbc \
            -out "$dir/ca.key"
    fi
    openssl req -x509 -new -key "$dir/ca.key" -sha256 -days 3650 \
        -subj "/CN=Cluster Web Internal CA" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -addext "subjectKeyIdentifier=hash" \
        -out "$dir/ca.pem"
    chmod 644 "$dir/ca.pem"
    echo "CA created: $dir/ca.pem (distribute) and $dir/ca.key (keep offline)"
    ;;
issue)
    [ $# -ge 1 ] || die "issue needs a name"
    name=$1; shift
    case $name in *[!A-Za-z0-9.-]*|"") die "invalid name: $name" ;; esac
    [ -f "$dir/ca.key" ] && [ -f "$dir/ca.pem" ] || die "no CA in $dir (run init-ca)"
    san=""
    if [ $# -eq 0 ]; then set -- "DNS:$name"; fi
    for s in "$@"; do
        case $s in DNS:*|IP:*) ;; *) die "SAN must be DNS:... or IP:..., got $s" ;; esac
        san="${san:+$san,}$s"
    done
    ext=$(mktemp)
    trap 'rm -f "$ext" "$dir/$name.csr"' EXIT
    cat >"$ext" <<CONF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=serverAuth
subjectAltName=$san
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
CONF
    openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$dir/$name.key"
    openssl req -new -key "$dir/$name.key" -subj "/CN=$name" -out "$dir/$name.csr"
    openssl x509 -req -in "$dir/$name.csr" -CA "$dir/ca.pem" -CAkey "$dir/ca.key" \
        -CAcreateserial -days "${CLUSTER_PKI_DAYS:-365}" -sha256 -extfile "$ext" \
        -out "$dir/$name.pem"
    chmod 644 "$dir/$name.pem"
    echo "issued $dir/$name.pem ($san); install $name.key on the server only"
    ;;
*)
    die "unknown command: $cmd"
    ;;
esac
