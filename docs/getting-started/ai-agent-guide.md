# AI Agent Guide for Comma Device Owners

Welcome! This guide will help you set up and use DoTPilot's AI-powered driving features on your comma 3 or comma 3X. No AI experience needed — we'll walk through everything step by step.

---

## What Does the AI Agent Do?

Your comma device already helps you drive — it handles adaptive cruise control, lane centering, and collision warnings. DoTPilot's AI agent adds a new layer on top of that: it **watches the road through the front camera** and **thinks about what it sees**, just like an experienced co-pilot sitting next to you.

Here's what that means in practice:

- **It sees a construction zone** ahead with orange cones and workers — it gently slows you down and shows an alert on your screen.
- **It checks Florida traffic data** and finds an accident 3 miles ahead on I-95 — it warns you and suggests reducing speed before you even see brake lights.
- **It spots a large pothole** in your lane — it alerts you, suggests slowing down, and records the pothole's location for road maintenance tracking.
- **A hurricane evacuation is ordered** in your area — it finds the nearest shelter, checks which roads are open, and can plan a route to safety.

Think of it as giving your comma device a brain that understands the road the way a human does — except it never gets tired, never gets distracted, and checks Florida traffic reports automatically.

### How Is This Different from Regular openpilot/sunnypilot?

Regular openpilot uses a specialized driving model that's excellent at steering and speed control but doesn't "understand" what it sees. It doesn't know what a construction cone means, can't read road signs contextually, and has no idea there's an accident reported 5 miles ahead.

The AI agent adds **understanding**. It uses a large language model (the same type of AI behind tools like Claude and ChatGPT) to look at camera images and driving data, reason about what's happening, and give intelligent advice to the driving system.

> **Important**: The AI agent is purely advisory. It suggests speed changes and shows alerts, but it never directly controls your steering, throttle, or brakes. Your comma device's existing safety systems remain fully in charge. If the AI agent ever crashes or disconnects, your car keeps driving normally — nothing changes.

---

## What You'll Need

| Item | Details |
|------|---------|
| **comma 3 or comma 3X** | The hardware device installed in your car |
| **DoTPilot installed** | DoTPilot (this fork of sunnypilot) loaded on your device |
| **Anthropic API key** | A key to access the Claude AI service (the AI brain) |
| **Internet connection** | Your device needs mobile data or Wi-Fi to reach the AI service |
| **FL511 API key** *(optional)* | For Florida traffic alerts — free to get |

### What Is an API Key?

An API key is like a password that lets your device talk to an AI service over the internet. When the AI agent wants to analyze what the camera sees, it sends the image to Anthropic's Claude service using this key. Anthropic charges a small fee for each use — typically a few cents per driving session.

You keep your API key private, just like a password. Never share it with anyone.

---

## Step 1: Get Your Anthropic API Key

The AI agent uses Anthropic's Claude to understand camera images and make driving decisions. You'll need to create an account and get an API key.

1. **Go to** [console.anthropic.com](https://console.anthropic.com/) on your phone or computer
2. **Create an account** (email + password, or sign in with Google)
3. **Add a payment method** — Anthropic charges based on usage. Typical driving use costs a few dollars per month depending on how often you drive. You can set a spending limit to avoid surprises.
4. **Create an API key**:
   - Click **API Keys** in the left sidebar
   - Click **Create Key**
   - Give it a name like "DoTPilot"
   - Copy the key — it starts with `sk-ant-` and is a long string of characters
5. **Save this key somewhere safe** — you'll need it in the next step. Anthropic only shows it once.

> **Cost tip**: The AI agent processes about 1 camera frame per second while driving. A typical 30-minute commute uses roughly $0.10–0.30 of API credits, depending on how much is happening on the road. You can set monthly spending limits in your Anthropic dashboard.

---

## Step 2: Get a Florida 511 API Key (Optional)

If you drive in Florida, this free API key unlocks real-time traffic intelligence — accidents, road closures, construction zones, and congestion data from Florida's 511 system. It also powers the evacuation routing feature during emergencies.

1. **Go to** [fl511.com/developers](https://fl511.com/developers)
2. **Register** for a developer account (free)
3. **Generate an API key** from your developer dashboard
4. **Save the key** — you'll enter it in the next step

If you don't drive in Florida, you can skip this. The AI agent still works without it — it just won't have access to Florida-specific traffic data.

---

## Step 3: Set Up the AI Agent on Your Device

You'll need to connect to your comma device to enter these settings. There are two ways to do this.

### Option A: Using SSH (Recommended)

If you have SSH set up (see [how to connect to your comma device](../how-to/connect-to-comma.md)), connect to your device and run these commands:

```bash
# Turn on the AI agent
echo -n "1" > /data/params/d/AgentEnabled

# Enter your Anthropic API key (replace with your actual key)
echo -n "sk-ant-api03-YOUR-KEY-HERE" > /data/params/d/AgentApiKey

# (Optional) Enter your FL511 API key for Florida traffic data
echo -n "YOUR-FL511-KEY-HERE" > /data/params/d/FL511ApiKey
```

That's it! The AI agent will start automatically the next time you begin a drive.

### Option B: Using the Python Shell

If you prefer, you can also set these values through Python:

```bash
# Open a Python shell on the device
cd /data/openpilot
python3 -c "
from openpilot.common.params import Params
p = Params()
p.put_bool('AgentEnabled', True)
p.put('AgentApiKey', 'sk-ant-api03-YOUR-KEY-HERE')
p.put('FL511ApiKey', 'YOUR-FL511-KEY-HERE')  # optional
print('AI Agent configured!')
"
```

### Turning It Off

If you ever want to disable the AI agent:

```bash
echo -n "0" > /data/params/d/AgentEnabled
```

The agent stops immediately. Your comma device continues working exactly as it did before — adaptive cruise, lane centering, and all other features are unaffected.

---

## Step 4: Go for a Drive!

Start your car and engage openpilot as usual. Once the system is running, the AI agent starts working automatically in the background. There's nothing extra to press or activate.

### What You'll See

The AI agent communicates through **alerts on your display** and **gentle speed adjustments**:

| What Happens | What You See | What the Car Does |
|-------------|-------------|-------------------|
| Construction zone detected | "Construction zone ahead" alert | Gradually slows down |
| Accident reported ahead (FL511) | "Accident on I-95 - 3mi ahead" alert | Reduces speed based on severity |
| Large pothole spotted | "Pothole ahead - caution" alert | Slows down, may suggest lane change |
| Slow vehicle ahead | "Slow vehicle ahead" alert | May suggest changing lanes |
| Evacuation zone active | "Evacuation zone - check route" alert | Provides shelter/route info |

Speed changes are always gradual — the car won't slam on the brakes. And remember, you're always in control. You can override any speed suggestion by pressing the gas pedal, just like with regular cruise control.

### What You Won't See

The AI agent works quietly. Most of the time, you won't notice anything different — it's checking and re-checking in the background. You'll only see alerts when there's something genuinely worth knowing about.

---

## AI Features Explained

### 1. Construction Zone Detection

**What it does**: The AI looks at the camera image and recognizes construction zones — orange cones, barriers, warning signs, construction vehicles, and road workers. When it spots these, it slows you down to a safe speed and shows an alert.

**How it works**: Every second or so, the AI receives a snapshot from your front camera. If it sees signs of construction, it calculates an appropriate speed reduction (typically 15–25 mph below your current speed, depending on the situation) and sends it to the driving system.

**Example**: You're cruising at 65 mph on I-95 and the AI spots orange construction barrels ahead. Your car smoothly reduces to about 45 mph and you see "Construction zone ahead" on your display.

### 2. Florida 511 Traffic Intelligence

**What it does**: Checks Florida's official traffic system for accidents, road closures, construction zones, and weather hazards near your location. It can spot trouble before you can see it.

**Requires**: FL511 API key (free — see Step 2)

**How it works**: The AI automatically checks the FL511 system every 30 seconds for traffic events near your GPS location. If it finds something significant — like a major accident 5 miles ahead on your road — it alerts you and adjusts your speed before you reach the problem.

**Severity levels**:
| Level | Example | What happens |
|-------|---------|-------------|
| **Critical** | Multi-vehicle accident blocking lanes | Significant speed reduction + red alert |
| **Major** | Serious incident, major delays | Moderate speed reduction + yellow alert |
| **Moderate** | Road work with lane closure | Mild speed reduction + yellow alert |
| **Minor** | Fender bender on shoulder | Information alert only |

### 3. Road Maintenance Detection

**What it does**: Spots road damage — potholes, cracks, debris, damaged signs, broken guardrails — and records each issue with its GPS location and a description. This turns every DoTPilot car into a road condition surveyor.

**How it works**: The AI looks at every camera frame for signs of road damage. When it spots something, it:
- Alerts you so you can avoid the hazard
- Records the exact GPS location, time, and a description
- Saves the report for potential submission to road maintenance agencies

**What it detects**:
- Potholes and pavement depressions
- Cracks in the road surface
- Debris in the travel lane (branches, tire fragments, rocks)
- Faded lane markings
- Damaged or missing road signs
- Broken guardrails
- Standing water (drainage problems)
- Shoulder erosion

For severe hazards (like a large pothole right in your lane), the AI will slow you down and may suggest moving to another lane.

### 4. Evacuation Routing (Florida)

**What it does**: During emergencies like hurricanes, this feature checks for active weather alerts and evacuation orders in your area, finds nearby shelters, and can plan a route to safety.

**Requires**: FL511 API key + active internet connection

**How it works**: The AI pulls data from three sources:
- **National Weather Service** — active weather alerts for your location
- **Florida Division of Emergency Management** — evacuation zones, open shelters, official evacuation routes
- **FL511** — road closures that might block your path

If a hurricane warning is active and your area is under evacuation orders, the AI can tell you which evacuation zone you're in, find the nearest open shelter, and plan a route that avoids closed roads.

> **Note**: This is an information tool, not a GPS navigator. It gives you the information to make good decisions — always follow official evacuation orders and use your best judgment.

### 5. Slow Vehicle Detection

**What it does**: When the AI sees a slow-moving vehicle ahead (like a truck climbing a hill or farm equipment on a rural road), it can suggest changing lanes to pass safely.

**How it works**: By analyzing the camera image, the AI recognizes vehicles moving significantly slower than traffic flow. It alerts you and may suggest moving to an adjacent lane if one is available.

---

## Frequently Asked Questions

### Is this safe?

Yes. The AI agent is designed with multiple layers of safety:

- **It's advisory only** — it suggests, the existing safety-validated driving system decides. It can never force the car to do anything.
- **It can only slow you down** — the AI can suggest lower speeds, but it cannot override safety systems to make you go faster.
- **It has built-in limits** — speed suggestions are capped at reasonable ranges and can't suddenly slam on the brakes (maximum 45 mph reduction at once).
- **If it fails, nothing happens** — if the AI crashes, loses internet, or encounters an error, your comma device keeps driving exactly as before. You won't even notice.
- **You're always in control** — you can override any AI suggestion instantly by pressing the gas pedal.

### How much does it cost to run?

The main cost is the Anthropic API usage. Typical costs:

| Driving pattern | Estimated monthly cost |
|----------------|----------------------|
| Short commute (15 min/day) | $2–5/month |
| Average commute (30 min/day) | $5–10/month |
| Highway road trip (several hours) | $1–3 per trip |

These are rough estimates. Actual costs depend on how much is happening on the road (more events = more AI processing). You can set spending limits in your Anthropic dashboard.

The FL511 API key is free. No other subscriptions are needed.

### Does it work outside Florida?

The core AI features — construction zone detection, slow vehicle detection, and road maintenance reporting — work **everywhere**. They only use the camera, which works on any road.

The Florida-specific features (FL511 traffic data and evacuation routing) only work in Florida. If you drive in another state, these features simply stay inactive — no errors, no impact.

### Does it need internet?

Yes, the AI agent needs an internet connection to send camera images to the AI service for analysis. If you lose signal:

- The AI agent pauses quietly
- Your comma device keeps driving normally with all its standard features
- When signal returns, the AI agent automatically resumes

The AI processes roughly 1 frame per second, so brief signal drops are handled gracefully.

### Does it send my camera images somewhere?

Yes — camera snapshots are sent to Anthropic's Claude service for analysis. These images are:
- Sent over an encrypted (HTTPS) connection
- Used only for real-time analysis — Anthropic does not store them for training
- Front camera only (road view) — never the driver-facing camera

This is similar to how navigation apps send your location to servers for route planning. If you're not comfortable with this, simply keep `AgentEnabled` set to `false` and no images will be sent.

### Can I use a different AI service?

The current version uses Anthropic's Claude, which provides the best combination of vision quality and speed for driving applications. Support for additional AI backends is planned for the future.

### What if I don't have an Anthropic API key?

Without an API key, the AI agent won't start. Your comma device works perfectly fine with all its standard openpilot/sunnypilot features — you're just not using the AI add-on.

### How do I know it's working?

After setup, go for a drive. If you drive through a construction zone, past some road damage, or near a traffic incident reported on FL511, you should see an alert on your display. If you drive on a quiet, clear road with no incidents, you may not see any alerts — that's normal! The AI only speaks up when there's something worth mentioning.

### Can I see the road maintenance reports it creates?

Yes! Reports are saved on your device. If you connect via SSH, you can view them:

```bash
# See the latest reports
cat /data/media/road_maintenance_log.jsonl | tail -5
```

Each line contains a JSON record with the GPS location, issue type, severity, and description of each road condition issue detected.

---

## Troubleshooting

### "FL511 API key not configured" alert

You're driving in Florida but haven't set up the FL511 API key. Either add the key (see Step 2) or ignore this — the other AI features still work.

### AI doesn't seem to be doing anything

This is usually normal! The AI works quietly and only shows alerts when it detects something noteworthy. Try these checks:

1. **Verify it's enabled**: `cat /data/params/d/AgentEnabled` should show `1`
2. **Verify the API key is set**: `cat /data/params/d/AgentApiKey` should show your key
3. **Check internet connection**: The device needs mobile data or Wi-Fi
4. **Drive through a construction zone or past visible road damage** — this should trigger an alert

### Speed seems too slow / too fast

The AI agent's speed suggestions are one input among many. The driving system always picks the lowest (safest) speed target from all sources. If you feel the AI is being too conservative, you can always press the gas pedal to override.

### I want to stop using the AI agent

Simply disable it:

```bash
echo -n "0" > /data/params/d/AgentEnabled
```

Everything goes back to normal instantly. Your API key stays saved — you can re-enable anytime by setting it back to `1`.

---

## Quick Reference

| Setting | Command |
|---------|---------|
| **Enable AI agent** | `echo -n "1" > /data/params/d/AgentEnabled` |
| **Disable AI agent** | `echo -n "0" > /data/params/d/AgentEnabled` |
| **Set Anthropic API key** | `echo -n "sk-ant-..." > /data/params/d/AgentApiKey` |
| **Set FL511 API key** | `echo -n "your-key" > /data/params/d/FL511ApiKey` |
| **View road reports** | `cat /data/media/road_maintenance_log.jsonl` |

---

## Glossary

| Term | What It Means |
|------|--------------|
| **AI agent** | The software that watches the road camera and makes intelligent driving suggestions |
| **LLM (Large Language Model)** | The type of AI that powers the agent — it can understand images and text, similar to ChatGPT or Claude |
| **API key** | A secret code that lets your device access the AI service — like a password for the AI |
| **Advisory** | A suggestion from the AI — it advises the car what to do, but doesn't force anything |
| **Tool** | A specific capability the AI can use (like checking FL511 traffic or reporting a pothole) |
| **Skill** | Knowledge that teaches the AI what to look for (like how to recognize a construction zone) |
| **FL511** | Florida's official traffic information system — reports accidents, closures, and construction |
| **HUD** | Heads-Up Display — the screen on your comma device where alerts appear |
| **SSH** | Secure Shell — a way to remotely connect to your comma device from a computer |
