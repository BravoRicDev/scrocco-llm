from __future__ import annotations

from datetime import datetime

from textual.containers import Vertical
from textual.widgets import DataTable, Label

from .gateway_client import GatewayClient, GatewayError


def _short_grp(g) -> str:
    if not g:
        return "-"
    if "__" in g:
        g = g.split("__", 1)[0]
    if len(g) > 28:
        g = "…" + g[-27:]
    return g


def _short_dep(d) -> str:
    if not d:
        return "-"
    d = d.rsplit("__", 1)[-1]
    if len(d) > 26:
        d = d[:25] + "…"
    return d


class LiveCallsPanel(Vertical):
    """Vista scorrimento chiamate live. Popolata da TASK B1."""

    _MAX = 500

    def __init__(self, client: GatewayClient):
        super().__init__()
        self.client = client
        self._last_ts: float | None = None
        self._loading: bool = False
        self._rows: list[tuple] = []          # cell-tuple, PIU' RECENTE in testa

    def compose(self):
        yield Label(
            "[b cyan]CHIAMATE LIVE[/]  [dim]auto 2s · piu' recenti in alto · "
            "via=provider reale · ttfb=primo token · giallo=fallback · "
            "magenta=QC · rosso=wd/err · esc chiudi[/]",
            id="obs-live-title",
        )
        yield DataTable(id="obs-live-t", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        t = self.query_one("#obs-live-t", DataTable)
        t.add_column("ora", key="ora")
        t.add_column("profilo", key="profile")
        t.add_column("gruppo", key="group")
        t.add_column("deployment", key="dep")
        t.add_column("modello", key="model")
        t.add_column("via", key="via")
        t.add_column("try", key="tries")
        t.add_column("fb", key="fb")
        t.add_column("ttfb", key="ttfb")
        t.add_column("ms", key="dur_ms")
        t.add_column("status", key="status")
        self._loading = False
        self.run_worker(self.refresh_data(), exclusive=True)
        self.set_interval(2.0, self._tick)

    def _tick(self) -> None:
        if not self._loading:
            self.run_worker(self.refresh_data(), exclusive=True)

    async def refresh_data(self) -> None:
        if self._loading:
            return
        self._loading = True
        try:
            try:
                data = await self.client.calls(tail=200, since=self._last_ts)
            except GatewayError as e:
                self.query_one("#obs-live-title", Label).update(
                    f"[red]errore gateway: {e}[/]"
                )
                return

            events = [ev for ev in data.get("events", [])
                      if ev.get("tag") == "summary"]
            if not events:
                return

            # gli eventi dall'endpoint sono in ordine crescente (vecchio->nuovo).
            # Inserendo ognuno in testa, il piu' recente del batch resta in cima.
            for ev in events:
                ora = datetime.fromtimestamp(int(ev["ts"])).strftime("%H:%M:%S") \
                    if ev.get("ts") else "-"
                prof = ev.get("profile") or "-"
                grp = _short_grp(ev.get("grp"))
                dep = _short_dep(ev.get("dep"))
                model = ev.get("model") or "-"
                raw = ev.get("raw") or {}
                via = ev.get("via") or raw.get("via")
                via = via if isinstance(via, str) and via else "-"
                tries = str(ev.get("tries") if ev.get("tries") is not None else "-")
                fb = ev.get("fb")
                fb_txt = str(fb) if fb is not None else "-"
                ttfb = str(ev.get("ttfb_ms") if ev.get("ttfb_ms") is not None else "-")
                ms = str(ev.get("dur_ms") if ev.get("dur_ms") is not None else "-")
                st = ev.get("status")
                st_txt = "-" if st in (None, 0) else f"[red]{st}[/]"

                # bad = fallback usato, status d'errore, QC scartato, watchdog
                qc = ev.get("qc") or raw.get("qc")
                wd = ev.get("wd") or raw.get("wd")
                try:
                    bad = (int(ev.get("fb") or 0) > 0) or (st not in (None, 0))
                except Exception:
                    bad = (st not in (None, 0))
                if bad:
                    dep = f"[yellow]{dep}[/]"
                if qc and isinstance(qc, (bool, int)) and bool(qc):
                    dep = f"[magenta]{dep}[/]"
                if wd and isinstance(wd, str) and wd:
                    dep = f"[red]{dep}‹{wd}›[/]"

                self._rows.insert(
                    0, (ora, prof, grp, dep, model, via, tries,
                        fb_txt, ttfb, ms, st_txt))

            del self._rows[self._MAX:]

            ts_vals = [ev["ts"] for ev in events if ev.get("ts")]
            if ts_vals:
                self._last_ts = max(ts_vals)

            t = self.query_one("#obs-live-t", DataTable)
            t.clear()
            for row in self._rows:
                t.add_row(*row)
            self.query_one("#obs-live-title", Label).update(
                f"[b cyan]CHIAMATE LIVE[/] [dim]({len(self._rows)}) · "
                f"auto 2s · esc[/]"
            )
        finally:
            self._loading = False
