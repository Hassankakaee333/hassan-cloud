# Self-improve (candidate)

Phone chat flow:
1. User: «حسّن التطبيق …» / «عدّل جزء من …»
2. App backs up current APK and shows an approval plan
3. User: «ابدأ»
4. Worker dispatches GitHub Actions job_type=`candidate_self_improve`
5. Runner builds `assembleCandidateDebug`, uploads `frishta-candidate-debug.apk`
6. Phone syncs job, downloads APK, prompts install
7. On failure / dislike: «ارجع للنسخة السابقة»

## Runner requirements

Set one of:
- GitHub Actions variable `HASSAN_CANDIDATE_REPO` = `owner/android-repo` containing this Android project
- or env `CANDIDATE_APP_ROOT` pointing at the Android project root
- or run from a monorepo checkout that includes `app/build.gradle.kts`

Without sources, the job fails with an honest `candidate_sources_missing` summary (no fake success).

## Signing note

`app/signing/candidate-ci.keystore` is a shared CI keystore prepared for aligning phone + Actions signatures later.
Until debug builds on the phone and CI use the same keystore, Android may reject an update install (`INSTALL_FAILED_UPDATE_INCOMPATIBLE`); rollback APK from the same signer still works.
