from taccom.alerts import Alert, Priority
from taccom.speech import SpeechQueue


def alert(text, priority):
    return Alert(text, text, priority, 0.0)


def test_most_urgent_first():
    q = SpeechQueue()
    q.push(alert("kill", Priority.LOW), 0.0)
    q.push(alert("hull", Priority.CRITICAL), 0.0)
    q.push(alert("shields", Priority.HIGH), 0.0)
    assert [q.next(0.1).text for _ in range(3)] == ["hull", "shields", "kill"]
    assert q.next(0.1) is None


def test_only_more_urgent_alerts_interrupt():
    q = SpeechQueue()
    q.push(alert("shields", Priority.HIGH), 0.0)
    assert q.next(0.1, speaking=Priority.HIGH) is None
    assert q.next(0.1, speaking=Priority.NORMAL).text == "shields"


def test_stale_alerts_are_dropped():
    q = SpeechQueue()
    q.push(alert("taking fire", Priority.NORMAL), 0.0)
    q.push(alert("hull critical", Priority.CRITICAL), 0.0)
    assert q.next(10.0).text == "hull critical"
    assert q.next(10.0) is None


def test_voice_cache_round_trip(tmp_path):
    np = __import__("pytest").importorskip("numpy")
    from taccom.speech import VoiceCache

    cache = VoiceCache(tmp_path)
    assert cache.get("Shields down.") is None
    cache.put("Shields down.", np.array([0.1, -0.2], dtype=np.float64))
    assert cache.has("Shields down.")
    assert cache.get("Shields down.").dtype == np.float32
    assert not cache.has("Shields back online.")
