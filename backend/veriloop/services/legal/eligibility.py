"""Legal-draft eligibility (BUILD_SPEC §7 rule 8, SPEC §8, G9).

Eligible iff verdict == MANIPULATED AND the subject is an identifiable individual AND, when the
subject is a private or unknown individual (`is_private_individual` True or None), the forensic
branch itself said MANIPULATED and >= 2 independent publisher groups document the manipulation.
Public figures still need MANIPULATED (which fusion only emits with >= 2 groups, rule 3).
"""

from __future__ import annotations

from veriloop.contracts import ForensicStatus, JobStatus, JobView, SubjectInfo, VerdictLabel

G9_MIN_GROUPS = 2


def legal_eligibility_parts(
    label: VerdictLabel | None,
    subject: SubjectInfo | None,
    forensic_status: ForensicStatus | None,
    groups_manipulation: int,
) -> tuple[bool, str | None]:
    if label is not VerdictLabel.MANIPULATED:
        shown = label.value if label else "none"
        return False, (
            "Legal drafts are only offered for MANIPULATED (synthetic/deepfake) media; "
            f"this verdict is {shown}."
        )
    if subject is None or not subject.is_identifiable_individual:
        return False, "No identifiable individual is depicted, so there is no person on whose behalf to file."
    if subject.is_private_individual is not False:  # private, or unknown -> treat as private (G9)
        who = (
            "a private individual"
            if subject.is_private_individual
            else "a person whose public status is unknown"
        )
        missing: list[str] = []
        if forensic_status is not ForensicStatus.MANIPULATED:
            shown = forensic_status.value if forensic_status else "unavailable"
            missing.append(f"forensic analysis must say MANIPULATED (it says {shown})")
        if groups_manipulation < G9_MIN_GROUPS:
            missing.append(
                f">= {G9_MIN_GROUPS} independent sources must document the manipulation "
                f"(found {groups_manipulation})"
            )
        if missing:
            return False, f"Extra caution (G9): the subject is {who}; " + " and ".join(missing) + "."
    return True, None


def legal_eligibility(job: JobView) -> tuple[bool, str | None]:
    """(eligible, block_reason) for a finished job."""
    if job.status is not JobStatus.COMPLETED:
        return False, "The verification has not finished yet."
    return legal_eligibility_parts(
        job.verdict,
        job.claim.subject if job.claim else None,
        job.forensic.status if job.forensic else None,
        job.fusion.groups_manipulation if job.fusion else 0,
    )


REPORTABLE = frozenset({VerdictLabel.FALSE, VerdictLabel.MANIPULATED})


def report_eligibility_parts(label: VerdictLabel | None) -> tuple[bool, str | None]:
    if label in REPORTABLE:
        return True, None
    shown = label.value if label else "none"
    if label is VerdictLabel.TRUE:
        return False, "The claim was verified as TRUE, so there is nothing to report."
    if label is VerdictLabel.UNVERIFIABLE:
        return False, "VeriLoop abstained (UNVERIFIABLE): reports need a FALSE or MANIPULATED verdict."
    return False, f"Reports are only prepared for FALSE or MANIPULATED verdicts (this one is {shown})."


def report_eligibility(job: JobView) -> tuple[bool, str | None]:
    if job.status is not JobStatus.COMPLETED:
        return False, "The verification has not finished yet."
    return report_eligibility_parts(job.verdict)
