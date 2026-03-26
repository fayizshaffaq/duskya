#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# MODULE: PACSTRAP (HARDWARE-VERIFIED & UNIFIED CACHE EDITION)
# AUTHOR: Elite DevOps Setup
# -----------------------------------------------------------------------------
set -euo pipefail

# --- Colors ---
if [[ -t 1 ]]; then
    readonly C_BOLD=$'\033[1m'
    readonly C_GREEN=$'\033[32m'
    readonly C_YELLOW=$'\033[33m'
    readonly C_RED=$'\033[31m'
    readonly C_RESET=$'\033[0m'
else
    readonly C_BOLD="" C_GREEN="" C_YELLOW="" C_RED="" C_RESET=""
fi

# --- Configuration ---
readonly MOUNT_POINT="/mnt"
USE_GENERIC_FIRMWARE=0
HW_CACHE=""
VM_DETECTED=0

# Base packages every system needs.
# Added 'linux-firmware-other' to catch minor USB/controllers missed by split packages.
FINAL_PACKAGES=(
    base base-devel linux linux-headers
    neovim btrfs-progs dosfstools git
    networkmanager yazi linux-firmware-other
)

# --- Logging Helpers ---
log_info() { echo -e "${C_GREEN}[INFO]${C_RESET} $*"; }
log_warn() { echo -e "${C_YELLOW}[WARN]${C_RESET} $*"; }
log_err()  { echo -e "${C_RED}[ERROR]${C_RESET} $*"; }

# --- Helper: Check if package exists in Arch Repos ---
package_exists() {
    pacman -Si "$1" &>/dev/null
}

# --- Helper: Unified Hardware Cache (PCI + USB) ---
get_hw_cache() {
    if [[ -z "$HW_CACHE" ]]; then
        local pci_data=""
        local usb_data=""

        # Bootstrapping dependencies silently
        if ! command -v lspci &>/dev/null; then
            pacman -S --noconfirm --needed pciutils &>/dev/null
        fi
        if ! command -v lsusb &>/dev/null; then
            pacman -S --noconfirm --needed usbutils &>/dev/null
        fi

        pci_data=$(lspci -mm 2>/dev/null) || pci_data=""
        usb_data=$(lsusb 2>/dev/null) || usb_data=""
        
        HW_CACHE=$(printf '%s\n%s' "$pci_data" "$usb_data")

        # Virtualization Guard (VirtIO, VMware, VirtualBox)
        if echo "$HW_CACHE" | grep -iEq "1af4|15ad|80ee|VirtualBox|VMware|VirtIO"; then
            VM_DETECTED=1
        fi
    fi
    printf '%s\n' "$HW_CACHE"
}

# --- Helper: Detect Hardware & Add Package ---
detect_and_add() {
    local name="$1"
    local pattern="$2"
    local pkg="$3"

    echo -ne "   > Scanning for ${name}... "

    # Short-circuit if a Virtual Machine is detected
    if ((VM_DETECTED)); then
        echo -e "${C_YELLOW}SKIPPED (VM Environment)${C_RESET}"
        return 0
    fi

    if get_hw_cache | grep -iEq "$pattern"; then
        echo -e "${C_GREEN}FOUND${C_RESET}"

        # If fallback is already active, acknowledge hardware but don't queue
        if ((USE_GENERIC_FIRMWARE)); then
             echo -e "     -> ${C_YELLOW}Generic mode active; bypassing specific package request.${C_RESET}"
             return 0
        fi

        if package_exists "$pkg"; then
            echo -e "     -> Queuing Verified Package: ${C_BOLD}${pkg}${C_RESET}"
            FINAL_PACKAGES+=("$pkg")
        else
            echo -e "     -> ${C_YELLOW}Hardware found, but package '$pkg' missing in repo.${C_RESET}"
            echo -e "     -> Switching to Safe Mode (Generic Firmware)."
            USE_GENERIC_FIRMWARE=1
        fi
    else
        echo "NO"
    fi
}

# ==============================================================================
# 1. SAFETY PRE-FLIGHT CHECKS
# ==============================================================================
echo -e "${C_BOLD}=== PACSTRAP: HARDWARE-VERIFIED EDITION ===${C_RESET}"

if ((EUID != 0)); then
    log_err "This script must be run as root."
    exit 1
fi

if ! mountpoint -q "$MOUNT_POINT"; then
    log_err "$MOUNT_POINT is not a mountpoint. Mount your partitions first."
    exit 1
fi

echo -ne "[....] Checking network connectivity..."
if ! ping -c 1 -W 3 archlinux.org &>/dev/null; then
    echo -e "\r[${C_RED}FAIL${C_RESET}] Checking network connectivity"
    log_err "No internet connection. Cannot install packages."
    exit 1
fi
echo -e "\r[${C_GREEN} OK ${C_RESET}] Checking network connectivity"

# Wait for pacman lock (Resolves Live ISO Reflector race condition)
while [[ -f /var/lib/pacman/db.lck ]]; do
    log_warn "Waiting for pacman lock (reflector.service running?)..."
    sleep 3
done

log_info "Syncing package databases..."
pacman -Sy --noconfirm &>/dev/null

# ==============================================================================
# 2. CPU MICROCODE
# ==============================================================================
# Robust awk parsing that ignores tabs/spacing quirks
CPU_VENDOR=$(awk '/^vendor_id/ {print $3; exit}' /proc/cpuinfo)

case "$CPU_VENDOR" in
    GenuineIntel)
        log_info "CPU: Intel Detected"
        FINAL_PACKAGES+=("intel-ucode")
        ;;
    AuthenticAMD)
        log_info "CPU: AMD Detected"
        FINAL_PACKAGES+=("amd-ucode")
        ;;
    *)
        log_warn "Unknown CPU Vendor ($CPU_VENDOR). Proceeding without specific ucode."
        ;;
esac

# ==============================================================================
# 3. PERIPHERAL DETECTION (PCI & USB)
# ==============================================================================
log_info "Scanning Hardware Topography (PCI + USB)..."

# Prime the cache and check for VMs
get_hw_cache >/dev/null

if ((VM_DETECTED)); then
    log_warn "Virtual Machine detected. Bypassing bare-metal firmware discovery."
fi

# -- GRAPHICS --
detect_and_add "Nvidia GPU"        "10de|nvidia"            "linux-firmware-nvidia"
detect_and_add "AMD GPU (Modern)"  "1002|amdgpu|navi|rdna"  "linux-firmware-amdgpu"
detect_and_add "AMD GPU (Legacy)"  "\b(radeon|ati)\b"       "linux-firmware-radeon"

# -- NETWORKING & BLUETOOTH --
detect_and_add "Intel Network/BT"  "intel.*(network|wireless|bluetooth)|8086" "linux-firmware-intel"
detect_and_add "Mediatek WiFi/BT"  "mediatek"               "linux-firmware-mediatek"
detect_and_add "Broadcom WiFi/BT"  "broadcom"               "linux-firmware-broadcom"
detect_and_add "Atheros WiFi/BT"   "atheros"                "linux-firmware-atheros"
detect_and_add "Realtek Eth/WiFi"  "realtek|\brtl"          "linux-firmware-realtek"

# -- AUDIO --
detect_and_add "Intel SOF Audio"   "audio.*intel|8086"      "sof-firmware"
detect_and_add "Cirrus Logic Audio""cirrus"                 "linux-firmware-cirrus"

# ==============================================================================
# 4. FINAL PACKAGE ASSEMBLY
# ==============================================================================
if ((USE_GENERIC_FIRMWARE)); then
    log_warn "Fallback Triggered: Consolidating to generic linux-firmware."

    # Filter out specific firmware packages if they sneaked in before fallback triggered
    CLEAN_LIST=()
    for pkg in "${FINAL_PACKAGES[@]}"; do
        [[ "$pkg" == linux-firmware-* || "$pkg" == "sof-firmware" ]] || CLEAN_LIST+=("$pkg")
    done
    FINAL_PACKAGES=("${CLEAN_LIST[@]}" "linux-firmware")
else
    # Add the license file required by split packages (only if not running generic)
    if ! ((VM_DETECTED)); then
        FINAL_PACKAGES+=("linux-firmware-whence")
    fi
fi

# ==============================================================================
# 5. EXECUTION
# ==============================================================================
echo ""
echo -e "${C_BOLD}Final Package List:${C_RESET}"
printf '%s\n' "${FINAL_PACKAGES[@]}"
echo ""

read -r -p "Ready to run pacstrap? [Y/n] " confirm
if [[ ! "${confirm,,}" =~ ^(y|yes|)$ ]]; then
    log_warn "Aborted by user."
    exit 0
fi

echo "Installing..."
pacstrap -K "$MOUNT_POINT" "${FINAL_PACKAGES[@]}" --needed

echo -e "\n${C_GREEN}Pacstrap Complete.${C_RESET}"
