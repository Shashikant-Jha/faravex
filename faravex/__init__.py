"""Air-gapped semantic code search MCP server backed by a local EmbeddingGemma model.

Importing this package forces Hugging Face libraries into offline mode and (unless
FARAVEX_ALLOW_NETWORK=1) blocks all non-loopback network connections, so the
model can only ever be loaded from the local folder.
"""

import os

# Must run before any huggingface_hub / transformers / sentence_transformers import.
for _key in (
    "HF_HUB_OFFLINE",
    "TRANSFORMERS_OFFLINE",
    "HF_DATASETS_OFFLINE",
    "HF_HUB_DISABLE_TELEMETRY",
    "HF_HUB_DISABLE_IMPLICIT_TOKEN",
    "HF_HUB_DISABLE_UPDATE_CHECK",
):
    os.environ[_key] = "1"
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TQDM_DISABLE", "1")


def _block_network() -> None:
    """Defense in depth: refuse DNS lookups and connections to anything but loopback."""
    import ipaddress
    import socket

    def is_local(host) -> bool:
        if host is None:
            return True
        if isinstance(host, bytes):
            host = host.decode(errors="ignore")
        host = str(host)
        if host.lower() in ("localhost", ""):
            return True
        try:
            return ipaddress.ip_address(host.split("%")[0]).is_loopback
        except ValueError:
            return False

    def check(address) -> None:
        if isinstance(address, tuple) and not is_local(address[0]):
            raise ConnectionRefusedError(f"faravex is air-gapped: blocked connection to {address!r}")

    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    orig_getaddrinfo = socket.getaddrinfo

    def connect(self, address):
        check(address)
        return orig_connect(self, address)

    def connect_ex(self, address):
        check(address)
        return orig_connect_ex(self, address)

    orig_sendto = socket.socket.sendto

    def sendto(self, data, *args):  # UDP can send without connect()
        check(args[-1])
        return orig_sendto(self, data, *args)

    def getaddrinfo(host, *args, **kwargs):
        if not is_local(host):
            raise socket.gaierror(f"faravex is air-gapped: blocked DNS lookup for {host!r}")
        return orig_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto
    socket.getaddrinfo = getaddrinfo


if os.environ.get("FARAVEX_ALLOW_NETWORK") != "1":
    _block_network()
