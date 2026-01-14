# Magic Box - Xbox NFC Control Setup

**Created:** January 13, 2026
**Project:** Magic Box (musicfig)
**Location:** C:\Users\jenni\projects\musicfig

---

## Overview

This document describes the Xbox NFC control feature added to Magic Box, allowing NFC tags to power on an Xbox console and switch the Samsung TV to the Xbox input.

---

## Azure AD App Registration

**App Name:** Magic Box Xbox Control
**Client ID:** bbf46f9e-0f24-4ddc-9ce8-fd5a08b8f5e3
**Tenant:** Personal Microsoft Account

### Configuration
- Platform: Web
- Redirect URI: http://localhost:8080/auth/callback
- Allow public client flows: Enabled (required for Xbox Live OAuth)
- Live SDK support: Enabled

### Required Scopes
- Xboxlive.signin
- Xboxlive.offline_access

---

## Authentication Flow

The Xbox Web API uses Microsoft's Live Connect OAuth flow:

1. **Authorization Request** → login.live.com/oauth20_authorize.srf
2. **User Login** → Microsoft account authentication
3. **Callback** → localhost:8080/auth/callback with auth code
4. **Token Exchange** → login.live.com/oauth20_token.srf
5. **Xbox Live Auth** → user.auth.xboxlive.com/user/authenticate
6. **XSTS Token** → xsts.auth.xboxlive.com/xsts/authorize

### Token Storage
- **File:** xbox_tokens.json (in project root)
- **Contents:** access_token, refresh_token, client_id
- **Auto-refresh:** Tokens refresh automatically when expired

---

## Files Created/Modified

### New Files
| File | Purpose |
|------|---------|
| app/xboxctl.py | Xbox control module (power on/off, launch apps) |
| xbox_tokens.json | OAuth token storage |
| xbox_auth_debug.py | Authentication helper script |

### Modified Files
| File | Changes |
|------|---------|
| app/lego.py | Added Xbox NFC handler with TV navigation |
| tags.yml | Added Xbox configuration and NFC tag |

---

## Xbox Module (xboxctl.py)

### Key Functions
```python
sync_power_on(console_id=None)    # Power on Xbox
sync_power_off(console_id=None)   # Power off Xbox
sync_launch_app(app_id, console_id=None)  # Launch app/game
sync_get_consoles()               # List registered consoles
```

### Console Information
- **Name:** Game Room Xbox
- **Console ID:** F4000F3ACB4A6A80

---

## Samsung TV Integration

### SmartThings API Limitation
The SmartThings `setInputSource` command returns HTTP 200 but does NOT reliably switch the TV input. This appears to be a Samsung firmware limitation when watching Live TV.

### Solution: Menu Navigation
Instead of using direct input switching, we navigate the TV menu using remote control keys:

```
HOME → LEFT → DOWN → RIGHT → DOWN → DOWN → DOWN → OK
```

**Sequence Breakdown:**
1. HOME - Opens Samsung overlay
2. LEFT - Opens side menu
3. DOWN - Navigate to "Connected Devices"
4. RIGHT - Step into devices list
5. DOWN×3 - Navigate to Xbox (4th item)
6. OK - Select Xbox

### TV Details
- **IP Address:** 192.168.0.226
- **SmartThings Device ID:** fd76f584-727e-80be-7a08-0fd00873c535

---

## NFC Tag Configuration

### tags.yml Entry
```yaml
# Xbox Control Tags
"04a769da204b80":
    name: Xbox
    xbox: true
```

### Behavior When Tag Placed
1. Stops any playing music
2. Navigates TV to Xbox input (via menu sequence)
3. Powers on Xbox via Xbox Web API
4. Pad turns OLIVE (yellow-green) on success
5. Pad flashes RED on failure

---

## Configuration in tags.yml

```yaml
# Xbox Control Configuration
xbox_client_id: "bbf46f9e-0f24-4ddc-9ce8-fd5a08b8f5e3"
xbox_console_id: "F4000F3ACB4A6A80"

# TV Input (for reference, not used due to SmartThings limitation)
smartthings_xbox_input: "HDMI2"
```

---

## Troubleshooting

### "No consoles found"
- Enable "Remote features" on Xbox: Settings → Devices & connections → Remote features
- Set power mode to "Instant-on": Settings → General → Power options
- Ensure Xbox is signed in with the same Microsoft account

### TV doesn't switch input
- SmartThings direct input switching is unreliable
- Use menu navigation sequence instead (already implemented)
- Verify TV IP is correct (192.168.0.226)

### Authentication errors
- Re-run xbox_auth_debug.py to get fresh tokens
- Ensure Azure AD app has "Allow public client flows" enabled
- Check redirect URI matches exactly: http://localhost:8080/auth/callback

### WebSocket "unauthorized"
- Samsung TV WebSocket requires explicit authorization
- Go to TV Settings → General → External Device Manager
- Enable "Access Notification" and allow "MagicBox"

---

## Xbox Requirements

For Magic Box to control the Xbox:
1. ✓ Xbox must have "Remote features" enabled
2. ✓ Power mode must be "Instant-on" (not Energy Saving)
3. ✓ Same Microsoft account on Xbox and in authentication
4. ✓ Xbox must be on the same network

---

## Future Enhancements

- [ ] Add xbox_app parameter to launch specific games
- [ ] Create more NFC tags for different Xbox games
- [ ] Add Xbox power-off tag
- [ ] Implement WebSocket local control (faster than SmartThings)
