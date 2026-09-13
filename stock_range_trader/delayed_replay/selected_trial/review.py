"""Record an already performed issuer-document review; never infer a lot from master."""

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

from delayed_replay.serialization import digest, time_text
from delayed_replay.validation import ReplayContractError

from .contract import SYMBOLS
from .pipeline import save


def record_review(
    root, symbol, local_pdf, *, url, effective_from, article, source_page
):
    if (
        symbol not in SYMBOLS
        or not article
        or not source_page
        or effective_from > "2026-04-30"
    ):
        raise ReplayContractError("document_review_incomplete")
    expected = {
        "46890": "https://www.lycorp.co.jp/",
        "94320": "https://group.ntt/",
        "94340": "https://www.softbank.jp/",
    }[symbol]
    if not url.startswith(expected) or not source_page.startswith(expected):
        raise ReplayContractError("issuer_source_mismatch")
    root = Path(root)
    raw = Path(local_pdf).read_bytes()
    if not raw.startswith(b"%PDF"):
        raise ReplayContractError("issuer_pdf_required")
    sha = hashlib.sha256(raw).hexdigest()
    path = root / (sha + ".pdf")
    if path.exists():
        if path.read_bytes() != raw:
            raise ReplayContractError("document_bytes_changed")
    else:
        with path.open("xb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
    subject = dict(
        instrument=symbol, start="2026-04-30", end="2026-06-01", lot_size=100
    )
    return save(
        root,
        dict(
            schema="selected-lot-review-v1",
            **subject,
            subject_hash=digest(subject),
            source=url,
            review_reference="assistant-reviewed-published-dated-charter:" + article,
            reviewed_at=time_text(datetime.now(UTC)),
            documents=[
                dict(
                    sha256=sha,
                    url=url,
                    effective_from=effective_from,
                    article=article,
                    source_page=source_page,
                )
            ],
            limitation="retrospective_issuer_charter_review_not_contemporaneous_publication_or_signed_attestation",
        ),
    )
