# Self-improve (candidate)

Phone chat flow:
1. User: «حسّن التطبيق …» / «عدّل جزء من …»
2. App backs up current APK and shows an approval plan
3. User: «ابدأ»
4. Worker dispatches GitHub Actions job_type=`candidate_self_improve`
5. Runner:
   - checks out `HASSAN_CANDIDATE_REPO`
   - calls **Gemini** with the goal + key UI sources
   - **applies real file edits** (write/replace)
   - optionally pushes commits back (needs `HASSAN_CANDIDATE_TOKEN`)
   - bumps version, runs `assembleCandidateDebug`
   - uploads `frishta-candidate-debug.apk` + patch/report artifacts
6. Phone syncs job, downloads APK, prompts install
7. On failure / dislike: «ارجع للنسخة السابقة»

## Required secrets / vars

| Name | Where | Purpose |
|------|--------|---------|
| `HASSAN_API_URL` + `HASSAN_CALLBACK_SECRET` | Actions secrets | Job callbacks + **`/v1/internal/codegen`** (uses Worker `GEMINI_API_KEY`) |
| `GEMINI_API_KEY` | Cloudflare Worker secret | Real source edits via codegen |
| `GEMINI_API_KEY` | Actions secret (optional fallback) | Direct Gemini if Worker codegen unavailable |
| `GEMINI_MODEL` | Actions/Worker var (optional) | Default `gemini-3.5-flash-lite` |
| `HASSAN_CANDIDATE_REPO` | Actions var | e.g. `Hassankakaee333/FrishtaAI-candidate` |
| `HASSAN_CANDIDATE_TOKEN` | Actions secret (**required for source push**) | PAT/`gh` token with `repo` (or contents:write) on `HASSAN_CANDIDATE_REPO`. Without it jobs report `skip-push-no-token` and improvements are lost on the next build. |

If coding fails, the job **fails honestly** (`self_improve_code_apply_failed`) instead of shipping an unchanged APK.

## Runner requirements

Set one of:
- GitHub Actions variable `HASSAN_CANDIDATE_REPO` = `owner/android-repo`
- or env `CANDIDATE_APP_ROOT`
- or monorepo checkout that includes `app/build.gradle.kts`

## Signing note

`app/signing/candidate-ci.keystore` is prepared for aligning phone + Actions signatures later.
Until both use the same keystore, Android may reject update install (`INSTALL_FAILED_UPDATE_INCOMPATIBLE`).
