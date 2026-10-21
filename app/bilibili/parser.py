"""Input classification and an explicit escape from possible QQ-number mistakes."""
import unicodedata

from src.gatekeeper.models import CheckResult
from src.gatekeeper.parsing import _UID_MARKER, parse_bili_identity

from .client import ACCOUNT_INPUT_GUIDANCE


def mistaken_qq_input(answer: str, applicant_qq: str | None):
    if not applicant_qq:
        return None
    parsed = parse_bili_identity(answer)
    normalized = unicodedata.normalize('NFKC', ''.join(c for c in answer if unicodedata.category(c) != 'Cf'))
    if parsed.kind == 'uid' and not _UID_MARKER.search(normalized) and parsed.value == str(applicant_qq):
        return CheckResult('failed', '你填写的数字与申请QQ号一致，可能误填了QQ号；请检查B站昵称或UID。如确为你的B站UID，请填写UID:加该数字后重新申请。' + ACCOUNT_INPUT_GUIDANCE)
    return None
