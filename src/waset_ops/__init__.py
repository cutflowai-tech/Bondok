"""waset_ops — the single deterministic execution authority for Waset social publishing."""
from .captions import CaptionMixin
from .core import BONDOK, MONDAY, OWNER, Command, CoreMixin, Rejected
from .items import ItemsMixin
from .monitor import MonitorMixin
from .publish import PublishMixin
from .record import RecordMixin
from .sched import SchedMixin

__version__ = '1.0.0'


class Ops(CaptionMixin, MonitorMixin, PublishMixin, SchedMixin, RecordMixin, ItemsMixin, CoreMixin):
    """Facade combining all handler responsibilities over one Store."""


__all__ = ['Ops', 'Command', 'Rejected', 'OWNER', 'MONDAY', 'BONDOK', '__version__']
