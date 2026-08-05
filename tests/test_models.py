from agent_harness.models import ALLOWED_TRANSITIONS, RECEIVED, REFUSED, STATES, TERMINAL_STATES, new_task_id


def test_transitions_only_reference_known_states():
    for from_state, to_states in ALLOWED_TRANSITIONS.items():
        assert from_state in STATES
        for to_state in to_states:
            assert to_state in STATES


def test_terminal_states_have_no_outgoing_transitions():
    for state in TERMINAL_STATES:
        assert state not in ALLOWED_TRANSITIONS


def test_every_non_terminal_state_can_be_cancelled():
    for state, to_states in ALLOWED_TRANSITIONS.items():
        assert "cancelled" in to_states, state


def test_refused_is_terminal_and_reachable_only_from_received():
    # issue #16: refusals get their own terminal state instead of failed, and only ever
    # happen at classification time, so nothing else should be able to transition into it.
    assert REFUSED in STATES
    assert REFUSED in TERMINAL_STATES
    for from_state, to_states in ALLOWED_TRANSITIONS.items():
        if from_state == RECEIVED:
            assert REFUSED in to_states
        else:
            assert REFUSED not in to_states


def test_task_ids_are_unique_and_sortable():
    ids = {new_task_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(task_id.startswith("t20") and "-" in task_id for task_id in ids)
