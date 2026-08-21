# DoTPilot v2x-traffic-advisor-f511 Branch

This branch combines DoTPilot road inspection with Florida 511 traffic advisories for in-vehicle warnings. It builds on sunnypilot/openpilot and focuses on agency workflows where fleet vehicles both observe roadway conditions and receive agency context back in the cockpit.

All added intelligence is advisory. The LLM and traffic-advisory processes publish UI findings and warnings, but they do not directly control steering, throttle, or brakes.

## Branch Focus

| Area | Implementation | Current output |
| --- | --- | --- |
| Road inspection | `sunnypilot/llm_agent/llm_agent.py` | `LLMRoadInspection` JSON for the onroad road-inspection overlay. |
| Traffic advisories | `sunnypilot/traffic_advisor/traffic_advisor.py` | `TrafficAdvisory` JSON for the FL511 onroad overlay. |
| Fleet log export | `system/loggerd/dropbox_uploader.py` | Wi-Fi-only route uploads plus `DropboxUploadPendingCount`. |

## Road Inspection

Enable the road inspection agent with `LLMAgentEnabled` and `AgentApiKey`. This branch publishes structured `LLMRoadInspection` JSON, uses `gpt-4o-mini` by default, keeps runtime logs under `/data/llm-agent-test/`, and renders findings such as pavement defects, standing water, debris, shoulder damage, lane-marking problems, sign/signal issues, guardrail damage, bridge concerns, and work zones.

```bash
echo -n "1" > /data/params/d/LLMAgentEnabled
echo -n "sk-..." > /data/params/d/AgentApiKey
```

## FL511 Traffic Advisor

Enable the traffic advisor with `TrafficAdvisorEnabled`. The `traffic-advisor` process fetches public FL511 map layers, filters them on-device by valid Florida GPS, heading, current road, distance, route corridor, severity, and event type, then publishes the selected event to `TrafficAdvisory` for the onroad overlay.

```bash
echo -n "1" > /data/params/d/TrafficAdvisorEnabled
```

Supported event groups include closures, incidents, construction, congestion, disabled vehicles, road conditions, weather, special events, and message signs.

## Dropbox Fleet Log Export

Enable the optional Dropbox uploader with `EnableDropboxUploader`. Configure either `DropboxAccessToken` or the refresh-token trio `DropboxRefreshToken`, `DropboxAppKey`, and `DropboxAppSecret`. Route files upload only on Wi-Fi, are grouped as `<route>/<segment>/<file>`, and pending work is reported through `DropboxUploadPendingCount`.

```bash
echo -n "1" > /data/params/d/EnableDropboxUploader
echo -n "/DoTPilotDrives" > /data/params/d/DropboxUploadFolder
```

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

When enabled, road inspection sends forward-camera images to the configured OpenAI backend. The FL511 advisor fetches public event/map data and filters it locally. Dropbox export uploads route logs to the configured Dropbox account. Base sunnypilot/comma logging behavior still applies.

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
