# Rental Property Automation Plan
## 2308 Main St (Airbnb) & 2623 Burdette St (Mid-term Rental)
### Created: January 13, 2026

---

## Overview

This document outlines the smart home automation strategy for our two rental properties, leveraging NFC tags, Yale locks, Minut sensors, Philips Hue lighting, and Apple ecosystem devices to create seamless guest experiences and robust remote property management.

---

## Property Summary

| Property | Type | Address | Status |
|----------|------|---------|--------|
| 2308 Main St | Airbnb (Short-term) | Bellevue, NE 68005 | In service Dec 2024 |
| 2623 Burdette St | Mid-term Rental | Omaha, NE 68111 | In service Dec 2025 |

---

## Tech Inventory Per Property

| Device | Purpose | Both Properties |
|--------|---------|-----------------|
| Raspberry Pi + NFC Reader | Physical automation triggers | ✓ |
| Yale Smart Lock | Guest access, entry logging | ✓ |
| Minut Sensor | Noise, motion, smoke, device count (occupancy) | ✓ |
| Philips Hue Lights | Scene control, presence simulation | ✓ |
| Philips Hue Cameras | Visual monitoring, motion clips | ✓ |
| Apple TV | Guest entertainment, welcome displays | Planned |
| HomePod Mini | Audio, announcements | Planned |

---

## Minut Sensor Capabilities

The Minut device provides critical rental property monitoring:

- **Noise Monitoring**: Detect parties, loud gatherings, quiet hours violations
- **Motion Detection**: Activity when property should be vacant
- **Smoke Detection**: Safety alerts
- **Device Count**: WiFi device counting approximates occupancy (guest count verification)
- **Temperature/Humidity**: Pipe freeze prevention, comfort monitoring

---

## Automation Scenarios

### Guest Check-In Flow
1. Yale Lock: Guest code entered → "Guest arrived" event
2. Minut: Device count tracked → Verify matches booking
3. Hue: Welcome scene triggers → Warm, inviting lights
4. NFC tag inside door → Guest taps for house guide on TV
5. Owner notification: "2623: Guest checked in, 2 devices, all normal"

### Guest Checkout Flow
1. Scheduled time reached → Lock code expires automatically
2. Minut: Device count should drop to 0
3. Hue: All lights off, eco mode
4. Cameras: Armed for vacant property monitoring
5. Owner notification: Ready for turnover
6. Cleaning crew notified

### Party Detection Response
1. Minut: Noise exceeds 75dB for 10+ minutes
2. Minut: Device count exceeds booking (e.g., 12 devices, booking for 4)
3. Time check: After quiet hours (10 PM)
4. Auto-response: Alert to owner phone
5. Optional: Porch light flashes 3x as subtle warning
6. Escalation: Message guest via Airbnb platform

### Vacant Property Security
1. Minut: Motion detected when no booking active
2. Yale: No authorized entry logged
3. Hue cameras: Capture clip
4. Alert level escalates with each sensor confirmation
5. Response options: Flash lights, sound alarm, notify owner

### Cleaning Crew Access
1. Cleaner uses NFC tag at door
2. Yale: Temporary access granted
3. Hue: Bright cleaning lights activated
4. Time tracking: Entry/exit logged
5. Completion: Cleaner taps "Checkout Complete" tag
6. Minut: Captures baseline readings (noise, device count = 1)

---

## NFC Tag Assignments

| Tag Type | Location | Purpose |
|----------|----------|---------|
| Owner Master | Keychain | Full access at any property, owner mode |
| Cleaner Tags | Given to cleaning crew | Time-boxed access, triggers turnover mode |
| Maintenance Tag | For handyman | Access + logs entry, notifies owner |
| Emergency Tag | Hidden outside | Backup access, high-priority alert |
| Guest Welcome | Inside front door | Tap for house guide on TV |
| Quiet Mode | Living room | Dims lights, signals quiet hours started |
| Leaving Tag | By exit door | Arms sensors, eco mode, lights off |

---

## Property Status Dashboard Concept

```
┌─────────────────────────────────────────────────────────────────┐
│  RENTAL PROPERTIES                           📅 Jan 13, 2026    │
├─────────────────────────────────────────────────────────────────┤
│  2308 MAIN ST (Airbnb)              2623 BURDETTE (Mid-term)    │
│  ┌─────────────────────────┐        ┌─────────────────────────┐ │
│  │ Status: GUEST PRESENT   │        │ Status: GUEST PRESENT   │ │
│  │ 🔒 Lock: Secured        │        │ 🔒 Lock: Secured        │ │
│  │ 👥 Devices: 3 (booked 4)│        │ 👥 Devices: 2 (lease 2) │ │
│  │ 🔊 Noise: 45dB ✓        │        │ 🔊 Noise: 32dB ✓        │ │
│  │ 🌡️  Temp: 69°F          │        │ 🌡️  Temp: 71°F          │ │
│  │ 💨 Humidity: 42%        │        │ 💨 Humidity: 38%        │ │
│  │ 🚶 Motion: 3 min ago    │        │ 🚶 Motion: 12 min ago   │ │
│  │ 💡 Lights: Living Room  │        │ 💡 Lights: Bedroom      │ │
│  │ Checkout: Jan 15, 11am  │        │ Lease ends: Mar 1       │ │
│  └─────────────────────────┘        └─────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

---

## System Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                 PRIMARY HOME (206 Forest Dr)                      │
│   ┌────────────────────────────────────────────────────────┐     │
│   │  Central Dashboard / Monitoring Hub                     │     │
│   │  - View all property status                             │     │
│   │  - Receive alerts                                       │     │
│   │  - Override any property remotely                       │     │
│   └────────────────────────────────────────────────────────┘     │
└──────────────────────────┬───────────────────────────────────────┘
                           │ Cloud/API
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
       ┌─────────┐    ┌─────────┐    ┌─────────┐
       │  2308   │    │  2623   │    │ Future  │
       │ Airbnb  │    │Mid-term │    │ Props   │
       ├─────────┤    ├─────────┤    ├─────────┤
       │Pi + NFC │    │Pi + NFC │    │Pi + NFC │
       │Yale Lock│    │Yale Lock│    │Yale Lock│
       │Minut    │    │Minut    │    │Minut    │
       │Hue Light│    │Hue Light│    │Hue Light│
       │Hue Cams │    │Hue Cams │    │Hue Cams │
       └─────────┘    └─────────┘    └─────────┘
```

---

## API Integrations Required

| Service | API | Purpose |
|---------|-----|---------|
| Minut | REST API + Webhooks | Real-time sensor data, event triggers |
| Yale | August/Yale API | Lock status, remote lock/unlock |
| Philips Hue | Hue Bridge API | Light control, scenes |
| Airbnb | iCal feed | Booking calendar sync |

---

## Implementation Phases

### Phase 1: Foundation
- [ ] Install Pi + NFC reader at each property
- [ ] Configure Yale lock API integration
- [ ] Set up Minut webhooks for alerts
- [ ] Create basic NFC tag assignments

### Phase 2: Automation
- [ ] Build guest check-in/checkout automations
- [ ] Implement party detection response
- [ ] Create cleaning crew workflow
- [ ] Set up vacant property monitoring

### Phase 3: Dashboard
- [ ] Build unified property status dashboard
- [ ] Integrate all sensor data streams
- [ ] Create mobile alert system
- [ ] Add historical data tracking

### Phase 4: Enhancement
- [ ] Add Apple TV welcome displays
- [ ] Implement HomePod announcements
- [ ] Create guest feedback collection
- [ ] Build predictive maintenance alerts

---

## Related Documents

- Magic Box NFC Tag Configuration (tags.yml)
- Knowledge Graph - Property nodes
- MCP Engineering Blueprint

---

## Notes

- Minut device count = WiFi devices connected, approximates occupancy
- Yale lock codes can be time-limited for guest stays
- Consider noise threshold differences: Airbnb (stricter) vs Mid-term (more lenient)
- 2623 is newer property (Dec 2025) - may need baseline period for normal readings

---

## TODO: Add to Google Docs

When Google Workspace MCP is available:
1. Create this as a Google Doc
2. Add to Master Document List (ID: 113hlTKLtTsBGkOBTZQy2_bZ19_65ACAlMJF4kWeRBPk)
3. Index in vector store
4. Add implementation tasks to Project Backlog
