import gzip
import io
import logging
from email.contentmanager import raw_data_manager
from email.message import EmailMessage
from typing import Callable, Generator, Mapping, Optional
from zipfile import ZipFile

from xsdata.formats.dataclass.context import XmlContext
from xsdata.formats.dataclass.parsers.xml import XmlParser

from dmarc_metrics_exporter.dmarc_event import (
    Disposition,
    DmarcEvent,
    DmarcResult,
    Meta,
)
from dmarc_metrics_exporter.model.dmarc_aggregate_report import (
    DispositionType,
    DkimresultType,
    DmarcresultType,
    Feedback,
    SpfresultType,
)


def handle_application_gzip(gzip_bytes: bytes) -> Generator[str, None, None]:
    yield gzip.decompress(gzip_bytes).decode("utf-8")


def handle_application_zip(zip_bytes: bytes) -> Generator[str, None, None]:
    with ZipFile(io.BytesIO(zip_bytes), "r") as zip_file:
        for name in zip_file.namelist():
            with zip_file.open(name, "r") as f:
                yield f.read().decode("utf-8")


def handle_text_xml(content: str) -> Generator[str, None, None]:
    yield content


content_type_handlers: Mapping[str, Callable[..., Generator[str, None, None]]] = {
    "application/gzip": handle_application_gzip,
    "application/zip": handle_application_zip,
    "text/xml": handle_text_xml,
}


def get_aggregate_report_from_email(
    msg: EmailMessage,
) -> Generator[Feedback, None, None]:
    parser = XmlParser(context=XmlContext())
    has_found_a_report = False
    for part in msg.walk():
        if part.get_content_type() in content_type_handlers:
            handler = content_type_handlers[part.get_content_type()]
            content = raw_data_manager.get_content(part)
            has_found_a_report = True
            for payload in handler(content):
                yield parser.from_string(payload, Feedback)
    if not has_found_a_report:
        logging.warning(
            "Failed to extract report from email by %s with subject '%s'.",
            msg.get("from", "<from missing>"),
            msg.get("subject", "<no subject>"),
        )


def _map_disposition(disposition: Optional[DispositionType]) -> Disposition:
    if disposition is None:
        return Disposition.NONE_VALUE
    return {
        DispositionType.NONE_VALUE: Disposition.NONE_VALUE,
        DispositionType.QUARANTINE: Disposition.QUARANTINE,
        DispositionType.REJECT: Disposition.REJECT,
    }[disposition]


def convert_to_events(feedback: Feedback) -> Generator[DmarcEvent, None, None]:
    for record in feedback.record:
        if record.row is None:
            continue

        if feedback.report_metadata:
            reporter = feedback.report_metadata.org_name or ""
        else:
            reporter = ""

        if record.identifiers:
            from_domain = record.identifiers.header_from or ""
        else:
            from_domain = ""

        if record.auth_results and len(record.auth_results.dkim) > 0:
            dkim = record.auth_results.dkim[0]
            dkim_domain = dkim.domain or ""
            dkim_pass = dkim.result == DkimresultType.PASS_VALUE
        else:
            dkim_domain = ""
            dkim_pass = False

        if record.auth_results and len(record.auth_results.spf) > 0:
            spf = record.auth_results.spf[0]
            spf_domain = spf.domain or ""
            spf_pass = spf.result == SpfresultType.PASS_VALUE
        else:
            spf_domain = ""
            spf_pass = False

        if record.row.policy_evaluated:
            disposition = _map_disposition(record.row.policy_evaluated.disposition)
            dkim_aligned = (
                record.row.policy_evaluated.dkim == DmarcresultType.PASS_VALUE
            )
            spf_aligned = record.row.policy_evaluated.spf == DmarcresultType.PASS_VALUE
        else:
            disposition = Disposition.NONE_VALUE
            dkim_aligned = False
            spf_aligned = False

        yield DmarcEvent(
            count=record.row.count or 1,
            meta=Meta(
                reporter=reporter,
                from_domain=from_domain,
                dkim_domain=dkim_domain,
                spf_domain=spf_domain,
            ),
            result=DmarcResult(
                disposition=disposition,
                dkim_aligned=dkim_aligned,
                spf_aligned=spf_aligned,
                dkim_pass=dkim_pass,
                spf_pass=spf_pass,
            ),
        )
