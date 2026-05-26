# Lightsei iOS

Native iOS client for Lightsei (Phase 29). Thin client over the
existing FastAPI backend — no business logic, all calls go to
`https://api.lightsei.com`.

## Setup

```bash
# One-time, if you don't have xcodegen yet:
brew install xcodegen

# Generate the Xcode project from project.yml. Run this any time
# project.yml or Sources/ changes structurally (new files, settings
# tweaks). xcodegen is idempotent.
cd ios/Lightsei
xcodegen

# Open in Xcode.
open Lightsei.xcodeproj
```

## Build + run

- Pick an iOS Simulator target (any iPhone 14/15/16 model on iOS 16+).
- Hit Cmd+R. The simulator should boot and show the Lightsei
  scaffold screen.
- Hit Cmd+U for tests (none yet).

## Project layout

```
ios/Lightsei/
  project.yml          xcodegen config — source of truth for the
                       Xcode project. Edit this, not the generated
                       .xcodeproj.
  Sources/
    LightseiApp.swift  @main entry point + scene config.
    ContentView.swift  root SwiftUI view.
  Resources/
    Assets.xcassets/   app icon + accent color.
  Lightsei.xcodeproj/  GENERATED — gitignored.
```

## Phase 29 sub-tasks (per TASKS.md)

- 29.1: project + SwiftUI scaffolding *(this commit)*
- 29.2: auth flow — magic-link via universal links + Sign in with Apple
- 29.3: conversation surface — vendor list, threads, composer
- 29.4: APNS push notifications
- 29.5: TestFlight + App Store submission

Requires an Apple Developer account ($99/yr) before 29.2 (SiwA) +
29.4 (APNS) work on a real device. 29.1 + 29.3 run in the simulator
without one.
