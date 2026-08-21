# DoTPilot codex/force-onroad-llm Branch

This branch carries the Dropbox uploader work on the force-onroad/LLM development line. It builds on [sunnypilot](https://github.com/sunnyhaibin/sunnypilot), itself a fork of comma.ai's openpilot, and keeps the LLM road-scene assistant available while validating fleet-log export behavior.

The added features are advisory or data-export features. They do not directly control steering, throttle, or brakes.

## Branch Focus

Recent work on this branch includes `EnableDropboxUploader`, static-token and refresh-token Dropbox auth, Wi-Fi-only route uploads, route-grouped remote paths in the form `<route>/<segment>/<file>`, `DropboxUploadPendingCount`, and the `LLMAgentEnabled` plus `AgentApiKey` road-scene assistant carryover.

```bash
echo -n "1" > /data/params/d/EnableDropboxUploader
echo -n "sl...." > /data/params/d/DropboxAccessToken
echo -n "/DoTPilotDrives" > /data/params/d/DropboxUploadFolder
```

Refresh-token auth is also supported with `DropboxRefreshToken`, `DropboxAppKey`, and `DropboxAppSecret`. The uploader intentionally waits for Wi-Fi before uploading route files.

## llm-agent Carryover

The branch can still run the road-scene assistant:

```bash
echo -n "1" > /data/params/d/LLMAgentEnabled
echo -n "sk-..." > /data/params/d/AgentApiKey
```

Use `llm-agent` for current road-inspection UI work and `v2x-traffic-advisor-f511` for the combined road-inspection plus FL511 advisory workflow.

## 💭 Join our Community Forum
Join the official sunnypilot community forum to stay up to date with all the latest features and be a part of shaping the future of sunnypilot!
* https://community.sunnypilot.ai/

## Documentation
https://docs.sunnypilot.ai/ is your one stop shop for everything from features to installation to FAQ about the sunnypilot

## 🚘 Running on a dedicated device in a car
First, check out this list of items you'll need to [get started](https://community.sunnypilot.ai/t/getting-started-using-sunnypilot-in-your-supported-car/251).

## Installation
Next, refer to the sunnypilot community forum for [installation instructions](https://community.sunnypilot.ai/t/read-before-installing-sunnypilot/254), as well as a complete list of [Recommended Branch Installations](https://community.sunnypilot.ai/t/recommended-branch-installations/235).

## 🎆 Pull Requests
We welcome both pull requests and issues on GitHub. Bug fixes are encouraged.

Pull requests should be against the most current `master` branch.

## 📊 User Data

Dropbox export uploads route files to the configured Dropbox account. Confirm the agency's retention, access-control, and consent policies before enabling it on fleet devices. The llm-agent carryover can send forward-camera images to the configured OpenAI backend when enabled.

By default, sunnypilot uploads the driving data to comma servers. You can also access your data through [comma connect](https://connect.comma.ai/).

sunnypilot is open source software. The user is free to disable data collection if they wish to do so.

sunnypilot logs the road-facing camera, CAN, GPS, IMU, magnetometer, thermal sensors, crashes, and operating system logs.
The driver-facing camera and microphone are only logged if you explicitly opt-in in settings.

By using this software, you understand that use of this software or its related services will generate certain types of user data, which may be logged and stored at the sole discretion of comma. By accepting this agreement, you grant an irrevocable, perpetual, worldwide right to comma for the use of this data.

## Licensing

sunnypilot is released under the [MIT License](LICENSE). This repository includes original work as well as significant portions of code derived from [openpilot by comma.ai](https://github.com/commaai/openpilot), which is also released under the MIT license with additional disclaimers.

The original openpilot license notice, including comma.ai’s indemnification and alpha software disclaimer, is reproduced below as required:

> openpilot is released under the MIT license. Some parts of the software are released under other licenses as specified.
>
> Any user of this software shall indemnify and hold harmless Comma.ai, Inc. and its directors, officers, employees, agents, stockholders, affiliates, subcontractors and customers from and against all allegations, claims, actions, suits, demands, damages, liabilities, obligations, losses, settlements, judgments, costs and expenses (including without limitation attorneys’ fees and costs) which arise out of, relate to or result from any use of this software by user.
>
> **THIS IS ALPHA QUALITY SOFTWARE FOR RESEARCH PURPOSES ONLY. THIS IS NOT A PRODUCT.
> YOU ARE RESPONSIBLE FOR COMPLYING WITH LOCAL LAWS AND REGULATIONS.
> NO WARRANTY EXPRESSED OR IMPLIED.**

For full license terms, please see the [`LICENSE`](LICENSE) file.

## 💰 Support sunnypilot
If you find any of the features useful, consider becoming a [sponsor on GitHub](https://github.com/sponsors/sunnyhaibin) to support future feature development and improvements.


By becoming a sponsor, you will gain access to exclusive content, early access to new features, and the opportunity to directly influence the project's development.


<h3>GitHub Sponsor</h3>

<a href="https://github.com/sponsors/sunnyhaibin">
  <img src="https://user-images.githubusercontent.com/47793918/244135584-9800acbd-69fd-4b2b-bec9-e5fa2d85c817.png" alt="Become a Sponsor" width="300" style="max-width: 100%; height: auto;">
</a>
<br>

<h3>PayPal</h3>

<a href="https://paypal.me/sunnyhaibin0850" target="_blank">
<img src="https://www.paypalobjects.com/en_US/i/btn/btn_donateCC_LG.gif" alt="PayPal this" title="PayPal - The safer, easier way to pay online!" border="0" />
</a>
<br></br>

Your continuous love and support are greatly appreciated! Enjoy 🥰

<span>-</span> Jason, Founder of sunnypilot
