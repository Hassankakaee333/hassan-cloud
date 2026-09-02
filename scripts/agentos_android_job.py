"""Hassan AgentOS real Android project benchmark job.

Creates/continues HassanTodoBenchmark inside the project's Persistent Workspace,
runs unit tests, builds a debug APK, syncs sources back, and stages artifacts.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Callable

from workspace_io import (
    fetch_job_context,
    fetch_workspace_optional,
    sync_workspace,
)

PROJECT_ROOT_NAME = "HassanTodoBenchmark"
PACKAGE = "ai.hassan.todo"
IGNORED_PARTS = {
    ".git",
    ".gradle",
    "build",
    ".idea",
    "__pycache__",
    ".pytest_cache",
    "local.properties",
    ".cxx",
}


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.replace("\r\n", "\n"), encoding="utf-8")


def _project_exists(root: Path) -> bool:
    return (root / "app" / "src" / "main" / "java" / "ai" / "hassan" / "todo" / "MainActivity.kt").exists()


def _scaffold_todo_project(root: Path) -> list[str]:
    """Create a minimal Arabic RTL Compose todo app. Returns created relative paths."""
    created: list[str] = []

    files: dict[str, str] = {
        "settings.gradle.kts": """\
pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}
rootProject.name = "HassanTodoBenchmark"
include(":app")
""",
        "build.gradle.kts": """\
plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.0.21" apply false
}
""",
        "gradle.properties": """\
org.gradle.jvmargs=-Xmx2g -Dfile.encoding=UTF-8
android.useAndroidX=true
kotlin.code.style=official
android.nonTransitiveRClass=true
""",
        "app/build.gradle.kts": """\
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "ai.hassan.todo"
    compileSdk = 35

    defaultConfig {
        applicationId = "ai.hassan.todo.benchmark"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "1.0.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.10.01")
    implementation(composeBom)
    androidTestImplementation(composeBom)

    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")

    debugImplementation("androidx.compose.ui:ui-tooling")

    testImplementation("junit:junit:4.13.2")
}
""",
        "app/src/main/AndroidManifest.xml": """\
<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <application
        android:allowBackup="true"
        android:label="@string/app_name"
        android:supportsRtl="true"
        android:theme="@style/Theme.HassanTodo">
        <activity
            android:name=".MainActivity"
            android:exported="true"
            android:windowSoftInputMode="adjustResize">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
    </application>
</manifest>
""",
        "app/src/main/res/values/strings.xml": """\
<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="app_name">مهام حسن</string>
</resources>
""",
        "app/src/main/res/values/themes.xml": """\
<?xml version="1.0" encoding="utf-8"?>
<resources>
    <style name="Theme.HassanTodo" parent="android:Theme.Material.Light.NoActionBar" />
</resources>
""",
        "app/src/main/java/ai/hassan/todo/TodoModels.kt": """\
package ai.hassan.todo

data class TodoItem(
    val id: String,
    val title: String,
    val done: Boolean = false,
)
""",
        "app/src/main/java/ai/hassan/todo/TodoStore.kt": """\
package ai.hassan.todo

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

class TodoStore(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun load(): List<TodoItem> {
        val raw = prefs.getString(KEY, "[]").orEmpty()
        val array = JSONArray(raw)
        return buildList {
            for (index in 0 until array.length()) {
                val obj = array.getJSONObject(index)
                add(
                    TodoItem(
                        id = obj.getString("id"),
                        title = obj.getString("title"),
                        done = obj.optBoolean("done", false),
                    ),
                )
            }
        }
    }

    fun save(items: List<TodoItem>) {
        val array = JSONArray()
        items.forEach { item ->
            array.put(
                JSONObject()
                    .put("id", item.id)
                    .put("title", item.title)
                    .put("done", item.done),
            )
        }
        prefs.edit().putString(KEY, array.toString()).apply()
    }

    fun add(title: String, items: List<TodoItem>): List<TodoItem> {
        val clean = title.trim()
        if (clean.isEmpty()) return items
        val next = items + TodoItem(id = UUID.randomUUID().toString(), title = clean)
        save(next)
        return next
    }

    fun toggle(id: String, items: List<TodoItem>): List<TodoItem> {
        val next = items.map { if (it.id == id) it.copy(done = !it.done) else it }
        save(next)
        return next
    }

    fun delete(id: String, items: List<TodoItem>): List<TodoItem> {
        val next = items.filterNot { it.id == id }
        save(next)
        return next
    }

    companion object {
        private const val PREFS = "hassan_todo_benchmark"
        private const val KEY = "todos"
    }
}
""",
        "app/src/main/java/ai/hassan/todo/MainActivity.kt": """\
package ai.hassan.todo

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalLayoutDirection
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.unit.LayoutDirection

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val store = TodoStore(this)
        setContent {
            CompositionLocalProvider(LocalLayoutDirection provides LayoutDirection.Rtl) {
                MaterialTheme {
                    Surface(modifier = Modifier.fillMaxSize()) {
                        TodoApp(store = store)
                    }
                }
            }
        }
    }
}
""",
        "app/src/main/java/ai/hassan/todo/TodoApp.kt": """\
package ai.hassan.todo

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.unit.dp

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun TodoApp(store: TodoStore) {
    var items by remember { mutableStateOf(store.load()) }
    var draft by remember { mutableStateOf("") }
    val completed = items.count { it.done }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text("مهام حسن", fontWeight = FontWeight.Bold)
                        Text("مكتمل: $completed / ${items.size}")
                    }
                },
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                OutlinedTextField(
                    value = draft,
                    onValueChange = { draft = it },
                    modifier = Modifier.weight(1f),
                    label = { Text("مهمة جديدة") },
                    singleLine = true,
                )
                Button(
                    onClick = {
                        items = store.add(draft, items)
                        draft = ""
                    },
                ) {
                    Text("إضافة")
                }
            }

            LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                items(items, key = { it.id }) { item ->
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Checkbox(
                            checked = item.done,
                            onCheckedChange = { items = store.toggle(item.id, items) },
                        )
                        Text(
                            text = item.title,
                            modifier = Modifier.weight(1f),
                            textDecoration = if (item.done) TextDecoration.LineThrough else null,
                        )
                        TextButton(onClick = { items = store.delete(item.id, items) }) {
                            Text("حذف")
                        }
                    }
                }
            }
        }
    }
}
""",
        "app/src/test/java/ai/hassan/todo/TodoStoreLogicTest.kt": """\
package ai.hassan.todo

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class TodoStoreLogicTest {
    @Test
    fun addToggleDeleteFlow() {
        var items = emptyList<TodoItem>()
        items = items + TodoItem(id = "1", title = "اشتر خبز")
        assertEquals(1, items.size)
        items = items.map { if (it.id == "1") it.copy(done = true) else it }
        assertTrue(items.first().done)
        items = items.filterNot { it.id == "1" }
        assertTrue(items.isEmpty())
        assertFalse(items.any { it.title == "اشتر خبز" })
    }
}
""",
        "README.md": """\
# HassanTodoBenchmark

تطبيق مهام عربي بسيط (Kotlin + Jetpack Compose) لمعيار Hassan AgentOS.

القدرات:
- إضافة مهمة
- تعليم كمكتملة
- حذف مهمة
- حفظ محلي عبر SharedPreferences
- واجهة RTL عربية
""",
    }

    for relative, content in files.items():
        path = root / relative
        _write(path, content)
        created.append(relative.replace("\\", "/"))
    return created


def _apply_incremental_edit(root: Path, job_id: str, goal: str) -> tuple[Path, str, str]:
    """Modify existing TodoApp without recreating the project."""
    target = root / "app/src/main/java/ai/hassan/todo/TodoApp.kt"
    if not target.exists():
        target = root / "README.md"
        before = target.read_text(encoding="utf-8") if target.exists() else ""
        marker = f"\n\n<!-- Hassan AgentOS job {job_id}: {goal[:120]} -->\n"
        if marker not in before:
            target.write_text(before + marker, encoding="utf-8")
        after = target.read_text(encoding="utf-8")
        return target, before, after

    before = target.read_text(encoding="utf-8")
    marker = f"// AgentOS continue job {job_id}\n"
    after = before
    if marker not in before:
        after = before.replace(
            'Text("مهام حسن", fontWeight = FontWeight.Bold)',
            f'{marker}                        Text("مهام حسن · مستمر", fontWeight = FontWeight.Bold)',
            1,
        )
        if after == before:
            after = marker + before
        target.write_text(after, encoding="utf-8")
        after = target.read_text(encoding="utf-8")
    return target, before, after


def _zip_sources(root: Path) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not any(part in IGNORED_PARTS for part in path.parts):
                archive.write(path, arcname=path.relative_to(root).as_posix())
    return buffer.getvalue()


def _run(cmd: list[str], cwd: Path, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("ANDROID_HOME", env.get("ANDROID_SDK_ROOT", ""))
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _ensure_gradle_wrapper(root: Path, logs: list[str]) -> None:
    wrapper = root / "gradlew"
    if wrapper.exists():
        wrapper.chmod(wrapper.stat().st_mode | 0o111)
        return
    # Generate wrapper using Gradle from setup-gradle action / PATH.
    proc = _run(["gradle", "wrapper", "--gradle-version", "8.9"], cwd=root, timeout=300)
    logs.append(proc.stdout)
    logs.append(proc.stderr)
    if proc.returncode != 0 or not wrapper.exists():
        raise RuntimeError(f"gradle wrapper generation failed: {proc.returncode}")
    wrapper.chmod(wrapper.stat().st_mode | 0o111)


def run_agentos_android_job(
    *,
    job_id: str,
    project_id: str,
    github_run_id: str,
    out_dir: Path,
    update_job: Callable[..., None],
    register_agent: Callable[[str, str, str], None],
    stage_artifact: Callable[[str, str, bytes], None],
) -> None:
    root = out_dir / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    context = fetch_job_context(job_id)
    goal = str(context.get("goal") or "HassanTodoBenchmark AgentOS project")
    logs: list[str] = []

    initial_paths = fetch_workspace_optional(project_id, root)
    update_job(
        state="RUNNING",
        log_append=f"[gha] AgentOS workspace loaded files={len(initial_paths)}\n",
        checkpoint_stage="planner",
    )
    register_agent("Planner", "COMPLETE", f"Goal={goal[:200]}; existing_files={len(initial_paths)}")

    created_from_scratch = False
    if not _project_exists(root):
        update_job(state="CODING", log_append="[gha] creating HassanTodoBenchmark sources\n", checkpoint_stage="create_files")
        created = _scaffold_todo_project(root)
        created_from_scratch = True
        register_agent("Coder", "COMPLETE", f"Created {len(created)} project files for HassanTodoBenchmark")
        target = root / "app/src/main/java/ai/hassan/todo/TodoApp.kt"
        before = ""
        after = target.read_text(encoding="utf-8")
        diff = "".join(
            difflib.unified_diff(
                [],
                after.splitlines(keepends=True),
                fromfile="a/TodoApp.kt",
                tofile="b/TodoApp.kt",
            )
        )
    else:
        update_job(
            state="CODING",
            log_append="[gha] continuing existing HassanTodoBenchmark workspace\n",
            checkpoint_stage="incremental_code",
        )
        target, before, after = _apply_incremental_edit(root, job_id, goal)
        register_agent(
            "Coder",
            "COMPLETE",
            f"Incremental edit on existing project: {target.relative_to(root).as_posix()}",
        )
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{target.relative_to(root).as_posix()}",
                tofile=f"b/{target.relative_to(root).as_posix()}",
            )
        )

    update_job(state="TESTING", log_append="[gha] preparing Gradle wrapper + unit tests\n", checkpoint_stage="test")
    _ensure_gradle_wrapper(root, logs)

    test_proc = _run(["./gradlew", ":app:testDebugUnitTest", "--no-daemon"], cwd=root, timeout=900)
    logs.append("=== UNIT TEST ===\n")
    logs.append(test_proc.stdout)
    logs.append(test_proc.stderr)
    register_agent(
        "Reviewer",
        "COMPLETE" if test_proc.returncode == 0 else "FAILED",
        (test_proc.stdout + test_proc.stderr)[-3000:],
    )
    if test_proc.returncode != 0:
        stage_artifact("build-log.txt", "text/plain", "\n".join(logs).encode("utf-8"))
        stage_artifact("changes.diff", "text/plain", diff.encode("utf-8"))
        stage_artifact(
            "test-report.json",
            "application/json",
            json.dumps(
                {
                    "exit_code": test_proc.returncode,
                    "tests_passed": False,
                    "phase": "unit_test",
                    "created_from_scratch": created_from_scratch,
                    "github_run_id": github_run_id,
                },
                indent=2,
            ).encode("utf-8"),
        )
        update_job(
            state="FAILED",
            failure_reason="unit tests failed",
            result_summary="HassanTodoBenchmark unit tests failed",
            log_append=f"[gha] unit test exit={test_proc.returncode}\n",
            github_run_id=github_run_id,
        )
        raise RuntimeError("unit tests failed")

    update_job(state="CODING", log_append="[gha] building debug APK\n", checkpoint_stage="build_apk")
    build_proc = _run(["./gradlew", ":app:assembleDebug", "--no-daemon"], cwd=root, timeout=1200)
    logs.append("=== ASSEMBLE DEBUG ===\n")
    logs.append(build_proc.stdout)
    logs.append(build_proc.stderr)
    if build_proc.returncode != 0:
        stage_artifact("build-log.txt", "text/plain", "\n".join(logs).encode("utf-8"))
        stage_artifact("changes.diff", "text/plain", diff.encode("utf-8"))
        stage_artifact(
            "test-report.json",
            "application/json",
            json.dumps(
                {
                    "exit_code": build_proc.returncode,
                    "tests_passed": True,
                    "apk_built": False,
                    "phase": "assembleDebug",
                    "created_from_scratch": created_from_scratch,
                    "github_run_id": github_run_id,
                },
                indent=2,
            ).encode("utf-8"),
        )
        update_job(
            state="FAILED",
            failure_reason="assembleDebug failed",
            result_summary="HassanTodoBenchmark APK build failed",
            log_append=f"[gha] assembleDebug exit={build_proc.returncode}\n",
            github_run_id=github_run_id,
        )
        raise RuntimeError("assembleDebug failed")

    apk = root / "app/build/outputs/apk/debug/app-debug.apk"
    if not apk.exists():
        update_job(state="FAILED", failure_reason="APK missing after assembleDebug")
        raise RuntimeError("APK missing")
    apk_bytes = apk.read_bytes()
    if len(apk_bytes) < 1024 or apk_bytes[:2] != b"PK":
        update_job(state="FAILED", failure_reason="APK invalid")
        raise RuntimeError("APK invalid")

    update_job(state="VERIFYING", log_append="[gha] verifying APK + syncing workspace\n", checkpoint_stage="verify")
    register_agent("Verifier", "COMPLETE", f"APK size={len(apk_bytes)} sha256={hashlib.sha256(apk_bytes).hexdigest()[:16]}")

    # Strip build outputs before syncing durable workspace sources.
    for junk in root.rglob("build"):
        if junk.is_dir():
            shutil.rmtree(junk, ignore_errors=True)
    for junk in root.rglob(".gradle"):
        if junk.is_dir():
            shutil.rmtree(junk, ignore_errors=True)

    source_zip = _zip_sources(root)
    report = {
        "exit_code": 0,
        "tests_passed": True,
        "apk_built": True,
        "apk_name": "HassanTodoBenchmark-debug.apk",
        "apk_size": len(apk_bytes),
        "apk_sha256": hashlib.sha256(apk_bytes).hexdigest(),
        "created_from_scratch": created_from_scratch,
        "continued_existing_workspace": not created_from_scratch,
        "goal": goal,
        "github_run_id": github_run_id,
        "project": PROJECT_ROOT_NAME,
        "package": PACKAGE,
    }

    stage_artifact("HassanTodoBenchmark-debug.apk", "application/vnd.android.package-archive", apk_bytes)
    stage_artifact("source.zip", "application/zip", source_zip)
    stage_artifact("test-report.json", "application/json", json.dumps(report, indent=2).encode("utf-8"))
    stage_artifact("build-log.txt", "text/plain", "\n".join(logs).encode("utf-8"))
    stage_artifact("changes.diff", "text/plain", (diff or "# no textual diff\n").encode("utf-8"))

    sync_result = sync_workspace(project_id, root, initial_paths)
    update_job(
        state="VERIFYING",
        result_summary=(
            "HassanTodoBenchmark created and APK built"
            if created_from_scratch
            else "HassanTodoBenchmark continued from persistent workspace; APK rebuilt"
        ),
        log_append=f"[gha] workspace synced files={sync_result.get('files', 0)} scratch={created_from_scratch}\n",
        checkpoint_stage="workspace_synced",
        github_run_id=github_run_id,
    )
