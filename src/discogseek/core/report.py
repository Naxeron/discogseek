"""
Plain audit report exporters (CSV, TXT).
"""

import csv
import logging
import time
from pathlib import Path
from typing import List, Any


logger = logging.getLogger(__name__)


class BaseReportExporter:
    """Helper utilities for exporting audit, scan, and download data."""

    @staticmethod
    def export_csv(headers: List[str], rows: List[List[Any]], output_path: Path) -> None:
        """Exports tabular data to CSV."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for row in rows:
                writer.writerow(row)
        logger.info(f"✔ Exported CSV report to: {output_path}")

    @staticmethod
    def export_text(lines: List[str], output_path: Path, header_title: str = "Report") -> None:
        """Exports lines to a plain text file with timestamped header."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"# {header_title}\n")
            f.write(f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            for line in lines:
                f.write(f"{line}\n")
        logger.info(f"✔ Exported text report to: {output_path}")
