# Disabling PosterBoard on iOS 16 (Rootless Jailbreak)

## Overview

PosterBoard (`com.apple.PosterBoard`) is an iOS 16+ system app that manages Lock Screen wallpapers, live/animated wallpapers, and Contact Posters. It can consume 200-300MB RAM even with static wallpapers, making it a target for optimization on low-RAM devices (e.g., iPhone 8 Plus with 2GB RAM).

**Note:** This guide is for rootless jailbreaks (Dopamine, etc.). Rootful jailbreaks have different file system permissions and paths.

---

## Prerequisites

### On Your Computer (Linux/macOS)

1. **OpenSSH client** installed (pre-installed on most systems)
2. **Network connectivity** to your iOS device on the same WiFi network
3. **SSH server** enabled on iOS device (via OpenSSH package from Cydia/Sileo)
4. **Password-based SSH** access configured (root password = `alpine` by default)

### On Your iOS Device

- Rootless jailbreak installed (tested on Dopamine)
- OpenSSH or similar SSH server installed
- Write access to `/var/jb/` (jailbreak overlay filesystem)

---

## Step 1: SSH Connection

### Method 1: Using SSH with Password (Recommended for Manual)

On your computer, open terminal and connect:

```bash
ssh root@<device-ip>
```

When prompted for password, enter: `alpine`

To find your device's IP:
- On iOS: Settings > WiFi > Tap your network > IP Address

### Method 2: SSH with Automated Password (For Scripts)

Create a fake SSH_ASKPASS script to automate password entry:

```bash
mkdir -p ~/.ssh/askpass
cat > ~/.ssh/askpass/ssh-askpass << 'EOF'
#!/bin/bash
echo "alpine"
EOF
chmod +x ~/.ssh/askpass/ssh-askpass
```

Then connect with:
```bash
SSH_ASKPASS=~/.ssh/askpass/ssh-askpass SSH_ASKPASS_REQUIRE=force ssh -o "StrictHostKeyChecking=no" -o "UserKnownHostsFile=/dev/null" root@<device-ip>
```

### Common SSH Connection Issues

| Issue | Solution |
|-------|----------|
| Connection timeout | Ensure device is on same network; check firewall |
| Permission denied | Verify password is correct; check SSH config |
| Too many auth failures | Use `-o PreferredAuthentications=password -o PubkeyAuthentication=no` flags |

---

## Step 2: Understanding PosterBoard Architecture

### What is PosterBoard?

PosterBoard is NOT a daemon. It is a **system application** (`AppDomain-com.apple.PosterBoard`) that manages wallpapers in iOS 16+.

### PosterBoard Process Details

**Main executable location:**
```
/Applications/PosterBoard.app/PosterBoard
```

**Launch method:** Via `launchd` as a user-level service (user 501/mobile)

**Launchd label:** `com.apple.PosterBoard`

### PosterBoard Plugins (XPC Services)

PosterBoard spawns multiple extension plugins for different wallpaper types:

| Plugin | Path | Purpose |
|--------|------|---------|
| CollectionsPoster | `/System/Library/PrivateFrameworks/WallpaperKit.framework/PlugIns/CollectionsPoster.appex/` | Standard wallpapers |
| PhotosPosterProvider | `/System/Library/PrivateFrameworks/PhotosUIPrivate.framework/PlugIns/PhotosPosterProvider.appex/` | Photo wallpapers |
| UnityPosterExtension | `/System/Library/PrivateFrameworks/UnityPoster.framework/PlugIns/UnityPosterExtension.appex/` | Unity (watch face) wallpapers |
| EmojiPosterExtension | `/System/Library/PrivateFrameworks/EmojiPoster.framework/PlugIns/EmojiPosterExtension.appex/` | Emoji wallpapers |
| GradientPosterExtension | `/System/Library/PrivateFrameworks/GradientPoster.framework/PlugIns/GradientPosterExtension.appex/` | Gradient/color wallpapers |
| WeatherPoster | `/private/var/containers/Bundle/Application/<UUID>/Weather.app/PlugIns/WeatherPoster.appex/` | Weather wallpapers |
| AegirPoster | `/System/Library/CoreServices/AegirProxyApp.app/PlugIns/AegirPoster.appex/` | Astronomy wallpapers |
| ExtragalacticPoster | `/System/Library/PrivateFrameworks/WatchFacesWallpaperSupport.framework/PlugIns/ExtragalacticPoster.appex/` | Galaxy wallpapers |

**All processes run as PPID=1** (launchd), meaning they are managed by the system and restart automatically when killed.

### Memory Usage

- PosterBoard main process: ~100-200MB RSS
- Each plugin: ~10-30MB RSS
- Total: Can exceed 300MB RAM

---

## Step 3: Investigation Commands

### Check if PosterBoard is Running
```bash
ps aux | grep -iE 'poster|wallpaper' | grep -v grep
```

### Check launchd Registration
```bash
launchctl list | grep -i poster
```

### Check Process Tree (shows PPID=1 for system-managed)
```bash
ps -ef | grep CollectionsPoster | grep -v grep
```

### Find App Location
```bash
ls -la /Applications/ | grep -i poster
```

### Check Rootless Overlay
```bash
ls -la /var/jb/
```

---

## Step 4: Methods That DON'T Work

### Method 1: Disabled Plist (Doesn't Work)
```bash
# Adding to disabled.plist doesn't work because PosterBoard is launched
# as a user-level XPC service, not a daemon
launchctl disable user/501/com.apple.PosterBoard  # Doesn't prevent XPC launches
```

**Why it fails:** PosterBoard is spawned via XPC (inter-process communication) from SpringBoard/system frameworks, not via standard launchd plist.

### Method 2: Renaming/Moving App Bundle (Read-Only)
```bash
mv /Applications/PosterBoard.app /Applications/PosterBoard.app.bak
```
**Error:** Read-only file system (even on rootless jailbreak)

### Method 3: Symlink to /dev/null on Overlay
```bash
# This doesn't work because the real plugin is in /System/
# which takes precedence over the overlay
ln -s /dev/null /var/jb/System/Library/PrivateFrameworks/WallpaperKit.framework/PlugIns/CollectionsPoster.appex
```
**Why it fails:** `/System/` is mounted read-only and takes precedence over `/var/jb/` overlay.

---

## Step 5: Working Solution - Kill Script Daemon

### Strategy

Since we cannot prevent PosterBoard from launching, we must continuously kill it. We create a background daemon that monitors and kills all PosterBoard processes.

### Step 5.1: Create Kill Script

SSH into your device and run:

```bash
cat > /var/jb/basebin/killposter << 'SCRIPT'
#!/bin/sh
while true; do
    killall -9 PosterBoard CollectionsPoster PhotosPosterProvider UnityPosterExtension EmojiPosterExtension GradientPosterExtension WeatherPoster AegirPoster ExtragalacticPoster 2>/dev/null
    sleep 5
done
SCRIPT
chmod +x /var/jb/basebin/killposter
```

### Step 5.2: Create Launchd Plist for Auto-Start

```bash
cat > /var/jb/Library/LaunchAgents/com.test.killposter.plist << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.test.killposter</string>
    <key>ProgramArguments</key>
    <array>
        <string>/var/jb/basebin/killposter</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
PLIST
```

### Step 5.3: Load the Service

```bash
launchctl bootstrap user/501 /var/jb/Library/LaunchAgents/com.test.killposter.plist
```

Or if already loaded:
```bash
launchctl unload /var/jb/Library/LaunchAgents/com.test.killposter.plist
launchctl load /var/jb/Library/LaunchAgents/com.test.killposter.plist
```

### Step 5.4: Verify

```bash
# Check killposter is running
ps aux | grep killposter | grep -v grep

# Check PosterBoard is NOT running
ps aux | grep -iE 'poster|wallpaper' | grep -v grep
```

Expected output from second command should show ONLY `killposter`, no PosterBoard processes.

---

## Step 6: Alternative - Manual Run (Without Auto-Start)

If you don't want auto-start, just run manually:

```bash
# Create script
cat > /var/jb/basebin/killposter << 'SCRIPT'
#!/bin/sh
while true; do
    killall -9 PosterBoard CollectionsPoster PhotosPosterProvider UnityPosterExtension EmojiPosterExtension GradientPosterExtension WeatherPoster AegirPoster ExtragalacticPoster 2>/dev/null
    sleep 5
done
SCRIPT
chmod +x /var/jb/basebin/killposter

# Run in background
nohup /var/jb/basebin/killposter > /dev/null 2>&1 &
```

---

## Step 7: Undoing the Changes

### To Stop and Remove Completely

```bash
# Stop the service
launchctl unload /var/jb/Library/LaunchAgents/com.test.killposter.plist

# Kill the process
killall -9 killposter

# Remove files
rm -f /var/jb/basebin/killposter
rm -f /var/jb/Library/LaunchAgents/com.test.killposter.plist
```

### After Undo

Restart your device. PosterBoard will work normally again.

---

## Verification Checklist

After setup, verify with these commands:

```bash
# 1. Killposter should be running
ps aux | grep killposter | grep -v grep
# Expected: Shows /var/jb/bin/sh /var/jb/basebin/killposter

# 2. PosterBoard should NOT be running
ps aux | grep -iE 'poster|wallpaper' | grep -v grep
# Expected: Shows only killposter, no CollectionsPoster/PosterBoard

# 3. Wait 10 seconds and check again
sleep 10
ps aux | grep -iE 'poster|wallpaper' | grep -v grep
# Expected: Same as step 2

# 4. Check service status
launchctl list | grep killposter
# Expected: Shows the service
```

---

## Impact Assessment

### What Still Works
- Static wallpapers display correctly
- Lock Screen wallpaper settings (may be slower to open)
- Normal SpringBoard functionality

### What May Break
- Live/animated wallpaper previews
- Contact Posters feature
- Some wallpaper customization options

### RAM Savings
- PosterBoard main: ~100-200MB
- Plugins: ~50-100MB
- **Total potential savings: ~150-300MB**

---

## Troubleshooting

### "Permission denied" when creating files
```bash
# Ensure you're root
whoami  # Should return "root"
```

### "Service already loaded" error
```bash
launchctl unload /var/jb/Library/LaunchAgents/com.test.killposter.plist
launchctl load /var/jb/Library/LaunchAgents/com.test.killposter.plist
```

### PosterBoard still appears
```bash
# Check if killposter is running
ps aux | grep killposter

# If not running, manually start
nohup /var/jb/basebin/killposter > /dev/null 2>&1 &

# Force kill all poster processes
killall -9 PosterBoard CollectionsPoster PhotosPosterProvider UnityPosterExtension EmojiPosterExtension GradientPosterExtension WeatherPoster AegirPoster ExtragalacticPoster
```

### After device restart, PosterBoard returns
```bash
# Re-run
nohup /var/jb/basebin/killposter > /dev/null 2>&1 &
launchctl load /var/jb/Library/LaunchAgents/com.test.killposter.plist
```

---

## Quick Reference Commands

| Action | Command |
|--------|---------|
| Connect via SSH | `ssh root@<ip>` |
| Check status | `ps aux \| grep -iE 'poster\|killposter' \| grep -v grep` |
| Manual kill | `killall -9 PosterBoard CollectionsPoster ...` |
| Start blocker | `nohup /var/jb/basebin/killposter > /dev/null 2>&1 &` |
| Stop blocker | `killall -9 killposter` |
| Remove all | `launchctl unload /var/jb/Library/LaunchAgents/com.test.killposter.plist && rm -f /var/jb/basebin/killposter /var/jb/Library/LaunchAgents/com.test.killposter.plist` |

---

## File Locations Summary

| File | Path | Purpose |
|------|------|---------|
| Kill script | `/var/jb/basebin/killposter` | Background process killer |
| Launchd plist | `/var/jb/Library/LaunchAgents/com.test.killposter.plist` | Auto-start configuration |
| PosterBoard app | `/Applications/PosterBoard.app/PosterBoard` | Main executable (don't modify) |
| Plugin 1 | `/System/Library/PrivateFrameworks/WallpaperKit.framework/PlugIns/CollectionsPoster.appex/` | Primary wallpaper plugin |

---

## SSH Connection Script (For Automation)

Save as `ssh_ios.sh`:

```bash
#!/bin/bash
IP="${1:-192.168.29.75}"
SSH_ASKPASS=~/.ssh/askpass/ssh-askpass SSH_ASKPASS_REQUIRE=force ssh -o "StrictHostKeyChecking=no" -o "UserKnownHostsFile=/dev/null" -o "ServerAliveInterval=5" "root@${IP}" "${2:-echo connected}"
```

Usage:
```bash
./ssh_ios.sh 192.168.29.75 "ps aux | grep poster"
```

---

## Credits

- Method discovered through trial and error on iOS 16.7 (iPhone 8 Plus, Dopamine jailbreak)
- PosterBoard architecture documented via SSH investigation
- Tested on rootless jailbreak (Dopamine)

---

## Changelog

| Date | Version | Changes |
|------|---------|---------|
| 2026-03-31 | 1.0 | Initial documentation |

---

*Last updated: 2026-03-31*
