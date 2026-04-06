"""Tests for dispatch.notifications -- priority ordering, queue ops."""

from dispatch.notifications import Notification, NotificationQueue


def _notif(priority, ts_offset=0.0, name="agent", voice="voice", text="msg"):
    return Notification(
        priority=priority,
        timestamp=1000.0 + ts_offset,
        agent_name=name,
        agent_voice=voice,
        text=text,
    )


class TestNotificationOrdering:
    def test_ordering_by_priority(self):
        """Urgent (0) should sort before normal (1)."""
        urgent = _notif(0, ts_offset=1.0)
        normal = _notif(1, ts_offset=0.0)
        assert urgent < normal

    def test_ordering_by_timestamp(self):
        """Same priority: earlier timestamp sorts first."""
        earlier = _notif(1, ts_offset=0.0)
        later = _notif(1, ts_offset=1.0)
        assert earlier < later

    def test_mixed_ordering(self):
        """urgent-new < normal-old < normal-new."""
        normal_old = _notif(1, ts_offset=0.0, text="old")
        urgent_new = _notif(0, ts_offset=2.0, text="urgent")
        normal_new = _notif(1, ts_offset=1.0, text="new")
        sorted_list = sorted([normal_old, urgent_new, normal_new])
        assert sorted_list[0].text == "urgent"
        assert sorted_list[1].text == "old"
        assert sorted_list[2].text == "new"


class TestNotificationQueue:
    async def test_queue_empty_initially(self):
        """New NotificationQueue should report empty."""
        q = NotificationQueue()
        assert q.empty() is True

    async def test_queue_put_and_get(self):
        """put then get_nowait should return the notification."""
        q = NotificationQueue()
        notif = _notif(1, text="hello")
        await q.put(notif)
        assert q.empty() is False

        result = q.get_nowait()
        assert result.text == "hello"
        assert q.empty() is True

    async def test_queue_priority_order(self):
        """Items should come out in priority order."""
        q = NotificationQueue()
        await q.put(_notif(1, ts_offset=0.0, text="normal"))
        await q.put(_notif(0, ts_offset=1.0, text="urgent"))

        first = q.get_nowait()
        second = q.get_nowait()
        assert first.text == "urgent"
        assert second.text == "normal"
