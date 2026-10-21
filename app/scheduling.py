"""Bounded five-field cron matching with an explicit IANA time zone."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


class CronSchedule:
    def __init__(self, expression, timezone_name='Asia/Shanghai'):
        fields = str(expression).split()
        if len(fields) != 5 or len(str(expression)) > 256:
            raise ValueError('cron 需要 分 时 日 月 周 五个字段')
        self.zone = ZoneInfo(str(timezone_name))
        self.values = [self._field(field, low, high) for field, low, high in zip(
            fields, (0, 0, 1, 1, 0), (59, 23, 31, 12, 7))]
        self.values[4] = {0 if day == 7 else day for day in self.values[4]}
        self.day_wild, self.weekday_wild = fields[2] == '*', fields[4] == '*'

    @staticmethod
    def _field(source, low, high):
        result = set()
        for item in source.split(','):
            parts = item.split('/')
            if len(parts) > 2:
                raise ValueError('无效的 cron 步进')
            step = int(parts[1]) if len(parts) == 2 else 1
            if step < 1 or step > high - low + 1:
                raise ValueError('无效的 cron 步进')
            span = parts[0]
            if span == '*':
                start, end = low, high
            elif '-' in span:
                start, end = map(int, span.split('-'))
            else:
                start = int(span)
                end = high if len(parts) == 2 else start
            if not low <= start <= end <= high:
                raise ValueError('cron 字段超出范围')
            result.update(range(start, end + 1, step))
        return result

    def matches(self, timestamp):
        local = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(self.zone)
        minute, hour, day, month, weekday = self.values
        if local.minute not in minute or local.hour not in hour or local.month not in month:
            return False
        day_match, weekday_match = local.day in day, (local.weekday() + 1) % 7 in weekday
        # Conventional cron: restricted day-of-month and weekday match by OR.
        if not self.day_wild and not self.weekday_wild:
            return day_match or weekday_match
        return day_match and weekday_match
