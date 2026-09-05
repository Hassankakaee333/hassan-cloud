from hassan_cloud.provider_ui_worker_compat import _GEMINI_FOREGROUND_PACKAGES


def test_current_google_app_gemini_foreground_package_is_accepted():
    assert "com.google.android.googlequicksearchbox" in _GEMINI_FOREGROUND_PACKAGES
    assert "com.google.android.apps.bard" in _GEMINI_FOREGROUND_PACKAGES


def test_compat_source_does_not_add_secret_or_paid_transport():
    source = open("hassan_cloud/provider_ui_worker_compat.py", encoding="utf-8").read().lower()
    assert "api.openai.com" not in source
    assert "generativelanguage.googleapis.com" not in source
    assert "set_secret_text" not in source
    assert "get_secure_input_key" not in source
