"""Original input-state traces; no native execution or durable recorder."""

import pytest
from tests.unit.infra.native_host.test_acp import BINDING
from tests.unit.infra.native_host.test_input_delivery import item, state

from octop.infra.native_host.context import HostUnavailable
from octop.infra.native_host.inputs import NativeInputState, OrderedInputProjection


@pytest.mark.parametrize("initially_paused", [False, True])
def test_pause_resume_requires_a_new_idle_to_active_boundary(initially_paused):
    projection = OrderedInputProjection(BINDING)

    def observe(value):
        projection.observe({"kind": "state", "state": value})

    observe(state(1))
    observe({**state(2, active=71, running=True, pending=[item()]), "paused": initially_paused})
    if initially_paused:
        with pytest.raises(HostUnavailable):
            projection.context()
    else:
        original = projection.context()
        # An ordinary queued input must not retarget the current batch.
        observe(state(3, active=71, running=True, pending=[item(), item(72, "queued", "queue")]))
        assert projection.context() is original
        observe({**state(4, active=71, running=True, pending=[item()]), "paused": True})
    with pytest.raises(HostUnavailable):
        projection.context()
    observe(state(5, active=71, running=True, pending=[item(), item(72, "queued", "queue")]))
    with pytest.raises(HostUnavailable):
        projection.context()

    observe(state(6, pending=[item(72, "queued", "queue")]))
    observe(state(7, active=72, running=True, pending=[item(72, "queued")]))
    current = projection.context()
    assert current.turn_id == 72 and current.state_revision == 7
    assert [entry.client_id for entry in current.inputs] == ["queued"]


@pytest.mark.parametrize("source", ["human", "peer", "subagent", "background", "scheduled"])
def test_source_vocabulary_matches_the_pinned_native_variants(source):
    value = state(1, pending=[{**item(), "source": source}])
    if source == "scheduled":
        with pytest.raises(HostUnavailable):
            NativeInputState.parse(value)
    else:
        assert NativeInputState.parse(value).pending[0].source == source
