from hassan_cloud.provider_ui_worker import _extract_marker, _node_center


def test_extracts_tool_call_from_accessibility_tree():
    tree = (
        '1|com.google.android.apps.bard|android.view.View|text=FRISHTA_TOOL:{"tool":"cloud.jobs.list","arguments":{}}|desc=|bounds=0 0 10 10|clickable=false|editable=false|password=false\n'
    )
    kind, payload = _extract_marker(tree)
    assert kind == "tool"
    assert '"cloud.jobs.list"' in payload


def test_extracts_final_summary_from_accessibility_tree():
    tree = '1|pkg|view|text=FRISHTA_FINAL:{"summary":"done"}|desc=|bounds=1 2 3 4|clickable=false|editable=false|password=false'
    kind, payload = _extract_marker(tree)
    assert kind == "final"
    assert '"done"' in payload


def test_finds_editable_and_send_centers():
    tree = (
        '1|pkg|EditText|text=|desc=|bounds=100 200 500 300|clickable=true|editable=true|password=false\n'
        '2|pkg|View|text=|desc=Send|bounds=900 200 1000 300|clickable=true|editable=false|password=false'
    )
    assert _node_center(tree, editable=True) == (300, 250)
    assert _node_center(tree, labels=("إرسال", "Send")) == (950, 250)


def test_source_has_no_provider_api_transport():
    source = open('hassan_cloud/provider_ui_worker.py', encoding='utf-8').read()
    lowered = source.lower()
    assert 'generativelanguage.googleapis.com' not in lowered
    assert 'api.openai.com' not in lowered
    assert 'api.deepseek.com' not in lowered
    assert 'set_secret_text' not in lowered
    assert 'get_secure_input_key' not in lowered
