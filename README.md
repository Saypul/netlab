# NetLab

Simulator jaringan berbasis web, mirip EVE-NG/PNETLab. Router (FRRouting) dan PC (Alpine)
berjalan sebagai container Docker. VM QEMU dari image `.qcow2` milikmu sendiri juga bisa dijalankan.
Setiap kabel atau switch adalah Linux bridge, sehingga link bisa ditambah atau dicabut saat lab berjalan.

## Menjalankan

    sudo ./run.sh                     # http://<server>:8765/
    cat data/initial_password         # login pertama: user admin + password acak

Sebagai service: `cp netlab.service /etc/systemd/system/ && systemctl enable --now netlab`.

Variabel env: `NETLAB_HOST` (default 0.0.0.0), `NETLAB_PORT` (8765), `NETLAB_JWT_TTL` (detik, default 43200), `NETLAB_DATA`.

## Cara pakai

- **Device**: seret device (dikelompokkan per Router, Switch, Firewall, PC/Host) ke lembar lab.
  Bisa juga klik device lalu klik lembar lab; tahan Shift untuk menaruh beberapa sekaligus.
- **Connect (L)**: klik dua node, atau node lalu switch. Interface kosong berikutnya dipakai otomatis.
- Double-click node yang sedang berjalan untuk membuka konsol (vtysh/bash/sh) di browser.
- Konfigurasi router tersimpan saat kamu menjalankan `write memory` di vtysh.
  **Wipe** mengembalikan node ke konfigurasi awalnya.
- **Demo lab**: R1–R3 menjalankan OSPF, PC1 (192.168.1.10) dan PC2 (192.168.3.10).

## Image QEMU (Cisco vIOS, MikroTik CHR, VyOS, dll.)

    apt install qemu-system-x86 qemu-utils
    mkdir -p data/images/qemu/vyos && cp vyos.qcow2 data/images/qemu/vyos/

Opsional `data/images/qemu/<nama>/netlab.json`:

    {"label": "VyOS 1.4", "category": "router", "nic": "virtio-net-pci", "disk_if": "virtio",
     "extra": [], "iface_prefix": "eth", "iface_start": 0}

`category` menentukan grup image di daftar **Device**: `router`, `switch`, `firewall`, atau `host`.
Tanpa category, image masuk grup Other. Image Cisco memakai `"nic": "e1000"`. Setiap node mendapat
overlay copy-on-write, jadi image dasarnya tidak ikut berubah. Konsolnya memakai serial
(telnet ke 127.0.0.1, dijembatani ke browser).

Contoh MikroTik CHR (sudah terpasang di `data/images/qemu/mikrotik-chr-7.25/`):

    {"label": "MikroTik CHR 7.25", "category": "router", "nic": "e1000", "disk_if": "ide",
     "iface_prefix": "ether", "iface_start": 1}

### Katalog perangkat

Daftar **Device** sudah berisi perangkat-perangkat berikut, dikelompokkan per tipe. Yang belum punya
image tampil abu-abu. Untuk mengaktifkannya, taruh `.qcow2` di folder `data/images/qemu/<kunci>-<versi>/`,
misalnya `data/images/qemu/fortigate-7.4.4/`. Setelan QEMU, RAM, dan nama interface diambil dari katalog.

| Grup | Kunci folder → perangkat |
|---|---|
| Router | `mikrotik` MikroTik CHR · `vyos` VyOS · `vios` Cisco vIOS · `c8000v` Catalyst 8000v · `openwrt` OpenWrt |
| Switch | `viosl2` Cisco vIOS-L2 · `cumulus` Cumulus VX |
| Firewall | `opnsense` OPNsense · `pfsense` pfSense CE · `fortigate` FortiGate VM · `asav` Cisco ASAv · `vsrx` Juniper vSRX · `paloalto` Palo Alto VM-Series |
| PC / Host | `linux` Linux VM · `windows` Windows |

Setelan katalog adalah default umum dan belum diuji per vendor, kecuali MikroTik CHR.
Semuanya bisa ditimpa lewat `netlab.json` di folder image. Sebelum start, VM dicek dulu: kalau RAM-nya
melebihi RAM bebas server dikurangi cadangan 256 MB (`NETLAB_RAM_RESERVE_MB`), node ditolak start.

## Cloud (koneksi ke jaringan luar)

Grup **Cloud** di daftar Device:

- **NAT Cloud (internet):** langsung bisa dipakai. Semua lab berbagi satu bridge `nlnat0` dengan gateway
  10.250.250.1/24 dan DHCP 10.250.250.128–254 (dnsmasq). Trafik keluar memakai alamat server, jadi node bisa
  menjangkau LAN server dan internet, sedangkan LAN tidak bisa menjangkau node.
  Node tidak bisa mengakses jaringan Docker maupun port Docker yang di-publish di server.
  Subnet bisa diubah dengan `NETLAB_NAT_SUBNET`.
- **Cloud pnet0 (server LAN):** bridge L2 langsung ke LAN server, seperti pnet0 di EVE-NG. Node tercolok ke LAN,
  bisa memakai IP dari LAN itu atau DHCP-nya, dan bisa diakses dari LAN.
  Fitur ini butuh bridge `pnet0` di server (lihat di bawah). Selama belum ada, entrinya tampil abu-abu.

### Membuat pnet0 (mengubah jaringan server, lakukan dengan hati-hati)

Kalau server hanya punya satu NIC, membuat `pnet0` berarti memindahkan IP server dari NIC itu ke bridge
`pnet0`. Kalau gagal, server tidak bisa diakses lewat jaringan. Kalau ada NIC cadangan tanpa IP, lebih aman
memakai NIC itu sebagai anggota `pnet0`.

Sebelum mulai:
1. Pastikan kamu punya akses console di luar jaringan (console hypervisor, IPMI, atau monitor langsung).
   Itulah jalan kembali kalau SSH terputus.
2. Kalau server adalah VM, izinkan MAC lain lewat NIC-nya. Di Proxmox: VM → Firewall → Options → MAC filter = No.
   Di VMware: aktifkan MAC address changes dan forged transmits. Kalau tidak, frame dari MAC node lab dibuang.
3. Pilih waktu sepi. Jaringan server terputus beberapa detik saat netplan diterapkan.

Langkah:

    cp host-network/01-netcfg-pnet0.example.yaml host-network/01-netcfg-pnet0.yaml
    nano host-network/01-netcfg-pnet0.yaml       # isi NIC, MAC, IP, gateway, DNS server ini
    cp /etc/netplan/01-netcfg.yaml host-network/01-netcfg.yaml.orig
    cp host-network/01-netcfg-pnet0.yaml /etc/netplan/01-netcfg.yaml
    netplan try --timeout 120      # tekan Enter kalau SSH masih jalan; kalau tidak, otomatis dikembalikan
    systemctl restart netlab       # Cloud pnet0 lalu muncul di grup Cloud

Netplan bisa memperingatkan bahwa perubahan bridge tidak selalu bisa dikembalikan otomatis.
Kalau server tidak bisa diakses, buka console lalu jalankan (dari folder NetLab):

    cp host-network/01-netcfg.yaml.orig /etc/netplan/01-netcfg.yaml
    netplan apply && ip link del pnet0

## Login dan user

Login memakai username/password, lalu server menerbitkan JWT (HS256) yang berlaku 12 jam.
- Password di-hash dengan PBKDF2-SHA256 dan disimpan di `data/users.json`. Kunci JWT ada di `data/jwt_secret`.
- Ganti password lewat tombol **Change password** di UI. Semua sesi lama user itu otomatis keluar.
- 5 kali gagal login dari IP yang sama memicu jeda 5 menit.

Kelola user dari server:

    cd /root/netlab
    .venv/bin/python auth.py list
    .venv/bin/python auth.py passwd <user>    # buat user baru / reset password (lupa password)
    .venv/bin/python auth.py deluser <user>

Untuk mengeluarkan semua sesi sekaligus: hapus `data/jwt_secret` lalu `systemctl restart netlab`.

## Keamanan

Server berjalan sebagai root dan mengendalikan Docker. Setiap user yang bisa login punya akses
setara root ke server ini, jadi berikan akun hanya kepada orang yang dipercaya.
Lalu lintas masih HTTP biasa: password dan JWT bisa disadap di jaringan. Untuk akses lewat internet,
pasang HTTPS (reverse proxy nginx/Caddy) atau set `NETLAB_HOST=127.0.0.1` dan akses lewat SSH tunnel.
