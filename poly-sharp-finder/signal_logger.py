"""
Logs detected signals to disk + stdout.

By default only actionable signal types are persisted to JSONL/CSV
(whale / convergence). Noisy book_imbalance / fast_move
are skipped on disk unless included in persist_types.
"""

import csv
import json
from dataclasses import asdict
from pathlib import Path
from datetime import datetime, timezone


class SignalLogger:
    def __init__(
        self,
        out_dir: str = "signals",
        *,
        persist_types: tuple[str, ...] | None = None,
        print_all: bool = False,
    ):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.jsonl_path = self.out_dir / f"polymarket_signals_{today}.jsonl"
        self.csv_path = self.out_dir / f"polymarket_signals_{today}.csv"
        self._csv_header_written = self.csv_path.exists()
        self.persist_types = persist_types  # None = persist everything
        self.print_all = print_all

    def log(self, signal):
        persist = (
            self.persist_types is None
            or signal.signal_type in self.persist_types
        )
        row = asdict(signal)
        row["detail"] = json.dumps(row["detail"])
        row["ts_readable"] = datetime.fromtimestamp(
            signal.ts, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")

        if persist:
            with open(self.jsonl_path, "a") as f:
                f.write(json.dumps(row) + "\n")

            write_header = not self._csv_header_written
            with open(self.csv_path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(row.keys()))
                if write_header:
                    writer.writeheader()
                    self._csv_header_written = True
                writer.writerow(row)

        if persist or self.print_all:
            print(
                f"[SIGNAL] {signal.signal_type:15s} | {signal.league:5s} | "
                f"{signal.label:30s} | side={signal.side} | {row['detail']}"
            )
