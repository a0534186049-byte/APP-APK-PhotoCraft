# PhotoCraft Android

Android build project for PhotoCraft, an open-source image editor (Photoshop-style workflow).

## Project identity

- Android application ID: `com.artcraft.photocraft`
- Android display name: `PhotoCraft`
- Fixed GitHub Release tag: `photocraft-latest`
- Release APK asset: `PhotoCraft.apk`
- Android builder: `MostafaXS1/artcraft-android` at immutable commit `2a03bb11c4d08e8ca3a5c96dc9fd942cbe7c29a9`
- PhotoCraft source: `storytold/photocraft` at immutable commit `75edf42c64dbd13b8d8e193d04fecb7f796b1e6e`

The upstream PhotoCraft source remains the application source of truth. This repository contains only the Android build workflow and PhotoCraft-specific Android preparation.

## Hebrew and Android adaptation

The build checks out the exact translation catalog commit recorded in PhotoCraft's `craft-i18n.rev`, embeds the Hebrew catalogs, and sets `CRAFT_I18N_REQUIRED=1`. The build fails rather than silently producing an English-only APK when the Hebrew catalog is missing.

The Android preparation also localizes the mobile text-action bar through PhotoCraft's own translation API and sizes its buttons to the available screen width. The pinned Android builder already supplies density-aware UI scaling, two-finger scroll synthesis, touch gesture handling, Android file pickers, and the app's original launcher icon.

Hebrew/RTL support in upstream PhotoCraft is not complete in every interior panel or dialog. The workflow runs the upstream Hebrew/RTL regression test and records the exact catalog coverage report; untranslated source strings continue to use the upstream English fallback.

## Build and release

- A push to `main` runs a debug build for CI verification.
- A manual `workflow_dispatch` can run either `debug` or `release`.
- A successful release build updates the single `photocraft-latest` Release and replaces `PhotoCraft.apk` rather than creating a new Release for each build.
- A backup Actions Artifact is also retained for 14 days.

For a signed release build, configure these repository Actions secrets with a dedicated PhotoCraft signing key (do not reuse another app's signing key):

- `ARTCRAFT_SIGNING_KEYSTORE_BASE64`
- `ARTCRAFT_SIGNING_PASSWORD`
- `ARTCRAFT_EXPECTED_CERT_SHA256`

The release workflow verifies the package ID, APK integrity, signing certificate, and the presence of the embedded Hebrew catalog before publishing the Release asset.

## Upstream

- PhotoCraft: https://github.com/storytold/photocraft
- ArtCraft Android Builder: https://github.com/MostafaXS1/artcraft-android
- Shared build and release workflow: https://github.com/a0534186049-byte/ArtCraft-Android-Common
