import json
import time
import urllib.error
import urllib.request

from . import config


class RpcError(Exception):
    pass


class Rpc:
    def __init__(self, url=None):
        self.url = url or config.RPC_URL
        self._id = 0

    def _post(self, body, timeout=90):
        data = json.dumps(body).encode()
        hdr = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
        delay = 2
        for attempt in range(8):
            try:
                time.sleep(config.RPC_PAUSE)
                req = urllib.request.Request(self.url, data, hdr)
                return json.load(urllib.request.urlopen(req, timeout=timeout))
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                code = getattr(e, "code", None)
                if code not in (None, 429, 500, 502, 503, 504, 521, 522, 524):
                    raise RpcError(f"HTTP {code}") from e
                time.sleep(delay)
                delay = min(delay * 2, 60)
        raise RpcError("узел не отвечает после 8 попыток")

    def call(self, method, params):
        self._id += 1
        r = self._post({"jsonrpc": "2.0", "method": method, "params": params, "id": self._id})
        if "error" in r:
            raise RpcError(r["error"].get("message", str(r["error"])))
        return r["result"]

    def batch(self, calls):
        """calls: [(method, params)] → результаты в том же порядке (None при ошибке)."""
        out = []
        for i in range(0, len(calls), 50):
            part = calls[i:i + 50]
            body = [{"jsonrpc": "2.0", "method": m, "params": p, "id": j} for j, (m, p) in enumerate(part)]
            res = self._post(body)
            byid = {x.get("id"): x for x in res} if isinstance(res, list) else {}
            out += [byid.get(j, {}).get("result") for j in range(len(part))]
        return out

    def block_number(self):
        return int(self.call("eth_blockNumber", []), 16)

    def block_time(self, n):
        return int(self.call("eth_getBlockByNumber", [hex(n), False])["timestamp"], 16)

    def logs(self, frm, to, address=None, topics=None):
        """eth_getLogs с автоматическим дроблением диапазона при отказе узла."""
        f = {"fromBlock": hex(frm), "toBlock": hex(to)}
        if address:
            f["address"] = address
        if topics:
            f["topics"] = topics
        try:
            return self.call("eth_getLogs", [f])
        except RpcError:
            if to - frm < 50:
                raise
            mid = (frm + to) // 2
            return self.logs(frm, mid, address, topics) + self.logs(mid + 1, to, address, topics)
