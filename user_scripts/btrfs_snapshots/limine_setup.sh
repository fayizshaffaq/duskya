#!/usr/bin/env bash
# Arch Linux (EFI + Btrfs root) | Limine core setup
# Bash 5.3+

set -Eeuo pipefail
export LC_ALL=C

AUTO_MODE=false
[[ "${1:-}" == "--auto" ]] && AUTO_MODE=true

declare -A BACKED_UP=()
declare -a EFFECTIVE_HOOKS=()
declare -A EFFECTIVE_HOOKS_SET=()

SUDO_PID=""
NEEDS_LIMINE_UPDATE=false

# Global Cache Invariants
CACHE_ESP_PATH=""
CACHE_ESP_PARTUUID=""
CACHE_ROOT_SOURCE=""
CACHE_EFIBOOTMGR_OUTPUT=""

cleanup() {
    kill "${SUDO_PID:-}" 2>/dev/null || true
}

trap_exit() {
    cleanup
}

trap_interrupt() {
    cleanup
    printf '\n\033[1;31m[FATAL]\033[0m Script interrupted by user.\n' >&2
    exit 130
}

trap 'printf "\n\033[1;31m[FATAL]\033[0m Script failed at line %d. Command: %s\n" "$LINENO" "$BASH_COMMAND" >&2; cleanup' ERR
trap trap_exit EXIT
trap trap_interrupt INT TERM HUP

fatal() {
    printf '\033[1;31m[FATAL]\033[0m %s\n' "$1" >&2
    exit 1
}

info() {
    printf '\033[1;32m[INFO]\033[0m %s\n' "$1"
}

warn() {
    printf '\033[1;33m[WARN]\033[0m %s\n' "$1" >&2
}

execute() {
    local desc="$1"
    shift

    if [[ "$AUTO_MODE" == true ]]; then
        "$@"
        return 0
    fi

    printf '\n\033[1;34m[ACTION]\033[0m %s\n' "$desc"
    read -r -p "Execute this step? [Y/n] " response || fatal "Input closed; aborting."
    if [[ "${response,,}" =~ ^(n|no)$ ]]; then
        info "Skipped."
        return 0
    fi

    "$@"
}

backup_file() {
    local file="$1"

    [[ -e "$file" ]] || return 0
    [[ -v BACKED_UP["$file"] ]] && return 0

    local stamp
    printf -v stamp '%(%Y%m%d-%H%M%S)T' -1

    sudo cp -a -- "$file" "${file}.bak.${stamp}"
    BACKED_UP["$file"]=1
    info "Backup created: ${file}.bak.${stamp}"
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || fatal "Required command not found: $1"
}

extract_subvol() {
    local opts="$1"
    local opt value
    local -a parts=()

    IFS=',' read -r -a parts <<< "$opts"
    for opt in "${parts[@]}"; do
        if [[ "$opt" == subvol=* ]]; then
            value="${opt#subvol=}"
            value="${value#/}"
            printf '%s\n' "$value"
            return 0
        fi
    done
    return 1
}

join_csv() {
    local IFS=,
    printf '%s' "$*"
}

get_root_source() {
    if [[ -n "$CACHE_ROOT_SOURCE" ]]; then
        printf '%s\n' "$CACHE_ROOT_SOURCE"
        return 0
    fi
    local source
    source="$(findmnt -no SOURCE / 2>/dev/null || true)"
    [[ -n "$source" ]] || return 1
    
    CACHE_ROOT_SOURCE="${source%%\[*}"
    printf '%s\n' "$CACHE_ROOT_SOURCE"
}

get_root_mount_opts() {
    findmnt -no OPTIONS /
}

get_root_subvolume_path() {
    local mount_opts path

    mount_opts="$(get_root_mount_opts)"
    path="$(extract_subvol "$mount_opts" || true)"
    if [[ -n "$path" ]]; then
        printf '%s\n' "$path"
        return 0
    fi

    require_cmd btrfs
    path="$(sudo btrfs subvolume show / 2>/dev/null | grep -E '^[[:space:]]*Path:' | awk '{print $2}' || true)"
    path="${path#/}"

    case "$path" in
        ""|"<FS_TREE>"|"/") return 1 ;;
    esac

    printf '%s\n' "$path"
}

build_btrfs_rootflags() {
    local opts="$1"
    local root_subvol="$2"
    local opt
    local -a parts=()
    local -a flags=()

    if [[ -n "$root_subvol" ]]; then
        flags+=("subvol=/${root_subvol#/}")
    fi

    IFS=',' read -r -a parts <<< "$opts"
    for opt in "${parts[@]}"; do
        case "$opt" in
            rw|ro|subvol=*|subvolid=*) continue ;;
            *) flags+=("$opt") ;;
        esac
    done

    if ((${#flags[@]} > 0)); then
        join_csv "${flags[@]}"
    fi
}

collect_mkinitcpio_files() {
    local -a files=("/etc/mkinitcpio.conf")
    local file
    local shopt_save

    shopt_save=$(shopt -p nullglob || true)
    shopt -s nullglob
    for file in /etc/mkinitcpio.conf.d/*.conf; do
        files+=("$file")
    done
    eval "$shopt_save"

    printf '%s\n' "${files[@]}"
}

get_effective_hooks() {
    local -a files=()
    mapfile -t files < <(collect_mkinitcpio_files)

    EFFECTIVE_HOOKS=()
    EFFECTIVE_HOOKS_SET=()

    local hooks_str
    hooks_str="$(
        env -i PATH="$PATH" LC_ALL=C bash -c '
            set -e
            for f in "$@"; do
                [[ -f "$f" ]] || continue
                source "$f"
            done
            # Convert scalar string to array if necessary
            [[ "$(declare -p HOOKS 2>/dev/null)" =~ "declare -a" ]] || HOOKS=(${HOOKS})
            printf "%s\n" "${HOOKS[@]}"
        ' bash "${files[@]}"
    )"

    [[ -n "$hooks_str" ]] || fatal "Could not determine the effective mkinitcpio HOOKS."

    mapfile -t EFFECTIVE_HOOKS <<< "$hooks_str"
    local hook
    for hook in "${EFFECTIVE_HOOKS[@]}"; do
        EFFECTIVE_HOOKS_SET["$hook"]=1
    done
}

hook_present() {
    [[ -v EFFECTIVE_HOOKS_SET["$1"] ]]
}

detect_esp_mountpoint() {
    if [[ -n "$CACHE_ESP_PATH" ]]; then
        printf '%s\n' "$CACHE_ESP_PATH"
        return 0
    fi

    if command -v bootctl >/dev/null 2>&1; then
        local esp
        esp="$(bootctl --print-esp-path 2>/dev/null || true)"
        if [[ -n "$esp" && -d "$esp" ]]; then
            CACHE_ESP_PATH="$esp"
            printf '%s\n' "$CACHE_ESP_PATH"
            return 0
        fi
    fi

    local candidate fstype
    for candidate in /efi /boot /boot/efi; do
        if mountpoint -q "$candidate"; then
            fstype="$(findmnt -M "$candidate" -no FSTYPE 2>/dev/null || true)"
            case "$fstype" in
                vfat|fat|msdos)
                    CACHE_ESP_PATH="$candidate"
                    printf '%s\n' "$CACHE_ESP_PATH"
                    return 0
                    ;;
            esac
        fi
    done

    return 1
}

get_mount_partuuid() {
    local mountpoint="$1"
    local source

    if [[ -n "$CACHE_ESP_PARTUUID" ]]; then
        printf '%s\n' "$CACHE_ESP_PARTUUID"
        return 0
    fi

    source="$(findmnt -M "$mountpoint" -no SOURCE 2>/dev/null || true)"
    [[ -n "$source" ]] || return 1

    CACHE_ESP_PARTUUID="$(sudo blkid -s PARTUUID -o value "$source" 2>/dev/null || true)"
    printf '%s\n' "$CACHE_ESP_PARTUUID"
}

set_shell_var() {
    local file="$1"
    local key="$2"
    local value="$3"
    local escaped_value

    escaped_value="${value//\\/\\\\}"
    escaped_value="${escaped_value//&/\\&}"
    escaped_value="${escaped_value//|/\\|}"

    sudo touch "$file"

    if sudo grep -qE "^[[:space:]]*${key}=" "$file"; then
        sudo sed -i -E "s|^[[:space:]]*${key}=.*|${key}=\"${escaped_value}\"|" "$file"
    else
        printf '%s="%s"\n' "$key" "$value" | sudo tee -a "$file" >/dev/null
    fi
}

dep_satisfied() {
    local dep="$1"
    ! pacman -T "$dep" >/dev/null 2>&1
}

choose_java_provider() {
    local pkg

    for pkg in jdk-openjdk jdk21-openjdk; do
        if pacman -Si "$pkg" >/dev/null 2>&1; then
            printf '%s\n' "$pkg"
            return 0
        fi
    done
    return 1
}

ensure_aur_build_prereqs() {
    local need_java=false
    local dep provider

    for dep in 'java-runtime>=21' 'java-environment>=21'; do
        if ! dep_satisfied "$dep"; then
            need_java=true
            break
        fi
    done

    [[ "$need_java" == true ]] || return 0

    provider="$(choose_java_provider)" || fatal "A Java provider for java-environment>=21 is required, but no suitable repo package was found."
    info "Installing $provider to satisfy Java build dependencies for the AUR package."
    sudo pacman -S --needed --noconfirm "$provider"
}

install_kernel_headers_if_needed() {
    local has_dkms=false
    local moddir pkgbase headers_pkg
    local shopt_save

    pacman -Q dkms >/dev/null 2>&1 && has_dkms=true
    
    shopt_save=$(shopt -p nullglob || true)
    shopt -s nullglob
    local dkms_dirs=(/var/lib/dkms/*)
    ((${#dkms_dirs[@]} > 0)) && has_dkms=true
    eval "$shopt_save"

    [[ "$has_dkms" == true ]] || return 0

    moddir="/usr/lib/modules/$(uname -r)"
    if [[ ! -r "${moddir}/pkgbase" ]]; then
        warn "DKMS detected, but ${moddir}/pkgbase was not found. Skipping header auto-install."
        return 0
    fi

    pkgbase="$(<"${moddir}/pkgbase")"
    headers_pkg="${pkgbase}-headers"

    if pacman -Q "$headers_pkg" >/dev/null 2>&1; then
        info "Kernel headers already installed: $headers_pkg"
        return 0
    fi

    if pacman -Si "$headers_pkg" >/dev/null 2>&1; then
        info "DKMS detected; installing matching kernel headers: $headers_pkg"
        sudo pacman -S --needed --noconfirm "$headers_pkg"
    else
        warn "DKMS detected, but no repo package named $headers_pkg was found."
    fi
}

install_repo_packages() {
    sudo pacman -S --needed --noconfirm \
        limine \
        efibootmgr \
        kernel-modules-hook \
        btrfs-progs

    install_kernel_headers_if_needed
}

install_aur_packages() {
    if pacman -Q limine-mkinitcpio-hook >/dev/null 2>&1; then
        info "limine-mkinitcpio-hook is already installed."
        return 0
    fi

    if ! command -v paru >/dev/null 2>&1 && ! command -v yay >/dev/null 2>&1; then
        fatal "No supported AUR helper found. Install paru or yay first."
    fi

    ensure_aur_build_prereqs

    if command -v paru >/dev/null 2>&1; then
        paru -S --needed --noconfirm --skipreview limine-mkinitcpio-hook
    else
        yay -S --needed --noconfirm \
            --answerdiff None \
            --answerclean None \
            --answeredit None \
            limine-mkinitcpio-hook
    fi
    NEEDS_LIMINE_UPDATE=true
}

configure_cmdline() {
    require_cmd btrfs

    local root_source root_type mount_opts root_subvol rootflags root_mode
    local mapper_name backing_dev luks_uuid root_uuid
    local tmp img
    local -a ucode_imgs=()
    local -a cmdline_parts=()

    get_effective_hooks

    root_source="$(get_root_source)"
    [[ -n "$root_source" ]] || fatal "Could not determine the root source device."

    root_type="$(lsblk -no TYPE "$root_source" 2>/dev/null | head -n1 || true)"
    [[ -n "$root_type" ]] || fatal "Could not determine block device type for $root_source"

    mount_opts="$(get_root_mount_opts)"
    root_subvol="$(get_root_subvolume_path || true)"
    rootflags="$(build_btrfs_rootflags "$mount_opts" "$root_subvol")"
    
    root_mode="rw"
    if [[ ",${mount_opts}," == *",ro,"* ]]; then
        root_mode="ro"
    fi

    cmdline_parts+=("${root_mode}" "rootfstype=btrfs")

    if [[ -n "$rootflags" ]]; then
        cmdline_parts+=("rootflags=${rootflags}")
    fi

    if [[ "$root_type" == "crypt" ]]; then
        require_cmd cryptsetup

        mapper_name="${root_source##*/}"
        backing_dev="$(sudo cryptsetup status "$root_source" 2>/dev/null | grep 'device:' | awk '{print $2}' || true)"
        [[ -n "$backing_dev" ]] || fatal "Root is on dm-crypt, but the backing LUKS device could not be determined."

        luks_uuid="$(sudo blkid -s UUID -o value "$backing_dev" 2>/dev/null || true)"
        [[ -n "$luks_uuid" ]] || fatal "Could not determine the LUKS UUID for $backing_dev"

        if hook_present sd-encrypt; then
            cmdline_parts+=("rd.luks.name=${luks_uuid}=${mapper_name}" "root=/dev/mapper/${mapper_name}")
        elif hook_present encrypt; then
            cmdline_parts+=("cryptdevice=UUID=${luks_uuid}:${mapper_name}" "root=/dev/mapper/${mapper_name}")
        else
            fatal "Root is on dm-crypt, but mkinitcpio has neither encrypt nor sd-encrypt in HOOKS."
        fi
    else
        root_uuid="$(findmnt -no UUID / 2>/dev/null || true)"
        [[ -n "$root_uuid" ]] || root_uuid="$(sudo blkid -s UUID -o value "$root_source" 2>/dev/null || true)"
        [[ -n "$root_uuid" ]] || fatal "Could not determine the Btrfs UUID for root."
        cmdline_parts+=("root=UUID=${root_uuid}")
    fi

    if ! hook_present microcode; then
        local shopt_save
        shopt_save=$(shopt -p nullglob || true)
        shopt -s nullglob
        ucode_imgs=(/boot/*-ucode.img)
        eval "$shopt_save"
        for img in "${ucode_imgs[@]}"; do
            cmdline_parts+=("initrd=/$(basename "$img")")
        done
    fi

    if [[ -n "${EXTRA_KERNEL_CMDLINE:-}" ]]; then
        cmdline_parts+=("${EXTRA_KERNEL_CMDLINE}")
    fi

    sudo mkdir -p /etc/kernel
    tmp="$(mktemp)"
    printf '%s\n' "${cmdline_parts[*]}" > "$tmp"

    if ! sudo cmp -s "$tmp" /etc/kernel/cmdline 2>/dev/null; then
        backup_file /etc/kernel/cmdline
        sudo install -m 0644 "$tmp" /etc/kernel/cmdline
        info "Updated /etc/kernel/cmdline"
        NEEDS_LIMINE_UPDATE=true
    else
        info "/etc/kernel/cmdline is already up to date."
    fi

    rm -f "$tmp"
}

configure_limine_defaults() {
    local limine_defaults="/etc/default/limine"
    local esp_target

    if [[ -f /etc/limine-entry-tool.conf && ! -f "$limine_defaults" ]]; then
        sudo install -m 0644 /etc/limine-entry-tool.conf "$limine_defaults"
    else
        sudo touch "$limine_defaults"
    fi

    esp_target="$(detect_esp_mountpoint)" || fatal "Could not detect a mounted ESP."

    local current_esp
    current_esp="$(grep -E '^[[:space:]]*ESP_PATH=' "$limine_defaults" 2>/dev/null | cut -d= -f2 | tr -d '"' || true)"

    if [[ "$current_esp" != "$esp_target" ]]; then
        backup_file "$limine_defaults"
        set_shell_var "$limine_defaults" ESP_PATH "$esp_target"
        info "Configured ESP_PATH=${esp_target} in $limine_defaults"
        NEEDS_LIMINE_UPDATE=true
    else
        info "$limine_defaults already has correct ESP_PATH."
    fi
}

load_efibootmgr_cache() {
    if [[ -z "$CACHE_EFIBOOTMGR_OUTPUT" ]]; then
        CACHE_EFIBOOTMGR_OUTPUT="$(sudo efibootmgr -v 2>/dev/null || true)"
    fi
}

get_boot_entries_for_loader_on_esp() {
    local loader_path="$1"
    local esp_partuuid="${2:-}"
    local line entry_code line_lc loader_lc partuuid_lc

    loader_lc="${loader_path,,}"
    partuuid_lc="${esp_partuuid,,}"
    
    load_efibootmgr_cache

    while IFS= read -r line; do
        [[ "$line" =~ ^Boot([0-9A-Fa-f]{4})\*?[[:space:]] ]] || continue
        entry_code="${BASH_REMATCH[1]^^}"
        line_lc="${line,,}"

        if [[ -n "$partuuid_lc" && "$line_lc" != *"gpt,${partuuid_lc},"* ]]; then
            continue
        fi
        [[ "$line_lc" == *"$loader_lc"* ]] || continue

        printf '%s\n' "$entry_code"
    done <<< "$CACHE_EFIBOOTMGR_OUTPUT"
}

has_loader_entry_on_esp() {
    local loader_path="$1"
    local esp_partuuid="${2:-}"
    local -a entries=()

    mapfile -t entries < <(get_boot_entries_for_loader_on_esp "$loader_path" "$esp_partuuid")
    ((${#entries[@]} > 0))
}

dedupe_named_limine_entries() {
    local esp_partuuid="${1:-}"
    local -a entries=()
    local keep entry

    mapfile -t entries < <(get_boot_entries_for_loader_on_esp '\EFI\limine\limine_x64.efi' "$esp_partuuid")
    ((${#entries[@]} > 1)) || return 0

    keep="${entries[0]}"
    warn "Multiple NVRAM entries point to \\EFI\\limine\\limine_x64.efi. Keeping Boot${keep} and deleting the extras."

    for entry in "${entries[@]:1}"; do
        if ! sudo efibootmgr -b "$entry" -B >/dev/null 2>&1; then
            warn "Failed to delete duplicate entry Boot${entry}."
        fi
    done
}

deploy_limine() {
    local esp_target esp_partuuid

    esp_target="$(detect_esp_mountpoint)" || fatal "Could not detect the ESP mount point."
    esp_partuuid="$(get_mount_partuuid "$esp_target" || true)"

    if [[ ! -f "${esp_target}/EFI/limine/limine_x64.efi" ]] || ! has_loader_entry_on_esp '\EFI\limine\limine_x64.efi' "$esp_partuuid"; then
        info "Installing Limine EFI entry."
        sudo limine-install
        NEEDS_LIMINE_UPDATE=true
    else
        info "Existing Limine EFI entry detected on the mounted ESP; skipping limine-install."
    fi

    if [[ "$NEEDS_LIMINE_UPDATE" == true || ! -f /boot/limine.conf ]]; then
        info "Refreshing Limine configuration..."
        sudo limine-update
    else
        info "Limine configuration is up to date; skipping redundant limine-update."
    fi

    dedupe_named_limine_entries "$esp_partuuid"

    [[ -f /boot/limine.conf ]] || fatal "Expected /boot/limine.conf was not created."
    info "Limine deployment and EFI entry cleanup completed successfully."
}

preflight_checks() {
    (( EUID != 0 )) || fatal "Run this script as a regular user with sudo privileges, not as root."

    require_cmd sudo
    require_cmd pacman
    require_cmd findmnt
    require_cmd mountpoint
    require_cmd blkid
    require_cmd lsblk
    require_cmd awk
    require_cmd sed
    require_cmd grep
    require_cmd cmp
    require_cmd mktemp

    [[ -d /sys/firmware/efi ]] || fatal "System is not booted in EFI mode."
    [[ -f /etc/mkinitcpio.conf ]] || fatal "/etc/mkinitcpio.conf was not found."
    [[ "$(stat -f -c %T /)" == "btrfs" ]] || fatal "Root filesystem is not Btrfs."

    sudo -v || fatal "Cannot obtain sudo privileges."
    (
        while true; do
            sudo -n -v 2>/dev/null || exit
            sleep 240
        done
    ) &
    SUDO_PID=$!
}

preflight_checks

execute "Install Limine core packages" install_repo_packages
execute "Generate /etc/kernel/cmdline" configure_cmdline
execute "Configure /etc/default/limine" configure_limine_defaults
execute "Install limine-mkinitcpio-hook from the AUR" install_aur_packages
require_cmd limine-install
require_cmd limine-update
execute "Deploy and finalize Limine" deploy_limine
