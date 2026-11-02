# NetLab

Simulator jaringan berbasis web, mirip EVE-NG/PNETLab. Router (FRRouting) dan PC (Alpine)
berjalan sebagai container Docker. VM QEMU dari image `.qcow2` milikmu sendiri juga bisa dijalankan.
Setiap kabel atau switch adalah Linux bridge, sehingga link bisa ditambah atau dicabut saat lab berjalan.

## Instalasi di server (Ubuntu / Debian)

Repo ini private, jadi installer butuh token GitHub yang punya akses baca ke `Saypul/netlab`.
Buat fine-grained token di GitHub → Settings → Developer settings, dengan izin *Contents: Read-only* untuk repo ini.

    TOKEN=github_pat_xxx
    curl -fsSL -H "Authorization: token $TOKEN" \
      https://raw.githubusercontent.com/Saypul/netlab/main/install.sh | sudo NETLAB_GITHUB_TOKEN=$TOKEN bash

Installer melakukan hal berikut:
- memasang Docker, QEMU, dnsmasq, dan Python;
- mengambil rilis terbaru ke `/opt/netlab`;
- membuat service `netlab` dan perintah `netlab`;
- menampilkan alamat web dan password awal user `admin`.

Variabel yang bisa diatur: `NETLAB_DIR`, `NETLAB_PORT` (default 8765), dan `NETLAB_REF` (tag/branch tertentu).

Perintah di server:

    netlab version | status | restart | logs
    netlab update --check     # cek rilis baru di GitHub
    netlab update             # update (minta konfirmasi; -y untuk langsung)
    netlab token              # ganti token GitHub
    netlab user list | passwd <user> | deluser <user>

Menjalankan manual tanpa installer: `sudo ./run.sh`, lalu buka `http://<server>:8765/`.
Password pertama ada di `data/initial_password`.

Variabel env: `NETLAB_HOST` (default 0.0.0.0), `NETLAB_PORT` (8765), `NETLAB_JWT_TTL` (detik, default 43200), `NETLAB_DATA`.

## Update online

- NetLab mengecek rilis baru di GitHub setiap 6 jam. Kalau ada, tombol **Update to x.y.z** muncul di kanan atas
  beserta catatan rilis dan daftar perubahannya. Dari CLI: `netlab update`.
- Proses update: ambil rilis baru, pasang dependency Python, restart service, lalu tunggu NetLab menyala.
  Kalau versi baru gagal menyala dalam 60 detik, NetLab otomatis kembali ke versi sebelumnya.
- Lab yang sedang jalan tetap jalan. Folder `data/` (lab, image VM, user, login AI, token) tidak disentuh.
- Update ditolak kalau ada perubahan kode lokal yang belum di-commit di folder instalasi.
- Status dan log update ada di `data/update.json` dan `data/update.log`.

Membuat rilis (dari mesin developer):

    ./release.sh 1.1.0 "Console VM dibagi dengan asisten AI"

Skrip ini menulis `VERSION`, commit, membuat tag `v1.1.0` (pesannya jadi catatan rilis), lalu push setelah
dikonfirmasi. Semua server NetLab kemudian melihat update tersebut.

Klik nomor versi di samping logo NetLab untuk mengecek update kapan saja. Kalau server belum punya token
GitHub, kolom token muncul di situ (sama dengan `netlab token`).

## Appliance VMware (.ova), seperti EVE-NG

Untuk Windows dan Mac Intel: satu file `NetLab-x.y.z.ova` yang tinggal di-import ke VMware Workstation
(Pro, sekarang gratis), Player, atau Fusion.

1. VMware: **File > Open**, pilih file `.ova`, beri nama, **Import**.
2. Sebelum dinyalakan: **Edit virtual machine settings > Processors**, centang
   **Virtualize Intel VT-x/EPT or AMD-V/RVI** (supaya VM router di dalam NetLab cepat). Atur RAM/CPU sesuai
   kebutuhan (default 8 GB, 4 CPU). Jaringan default NAT; pilih Bridged kalau mau diakses dari PC lain.
3. Nyalakan. Layar console menampilkan alamat yang dibuka di browser dan password admin pertama.
4. Login console/SSH: `root`, password pertama `netlab` (wajib diganti saat login pertama).

Catatan Windows: kalau Hyper-V, WSL2, atau Docker Desktop aktif, VMware tidak bisa memberi VT-x ke VM, jadi
VM router berjalan lambat. Router/PC container (FRR, Alpine) tidak terpengaruh.

Setiap salinan yang di-import membuat password admin, SSH host key, dan machine id sendiri saat boot pertama.
Appliance tidak berisi token GitHub: untuk update online, klik nomor versi di web lalu tempel token
(atau `netlab token` di console).

Membuat file `.ova` (di server Linux dengan KVM, sekitar 20 menit, perlu ~10 GB disk kosong):

    appliance/build-ova.sh v1.0.1      # hasil: dist/NetLab-1.0.1.ova

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

**Upload dari browser:** klik **Upload VM image** di atas daftar Device. Bisa juga dengan mengklik perangkat
abu-abu ("no image"), maka jenis perangkatnya langsung terpilih.
- Pilih jenis perangkat, isi versinya, lalu pilih file disk. Image disimpan di `data/images/qemu/<kunci>-<versi>/`.
- Untuk image di luar katalog, pilih **Other (custom image)** lalu isi nama, grup, RAM, jumlah interface,
  network card, dan disk bus.
- Jenis perangkat dan versi diisi otomatis dari nama file, misalnya `vyos-1.4.0-generic-amd64.iso` menjadi
  VyOS 1.4.0. Cek dulu sebelum upload.
- Format yang diterima: `.qcow2`, `.img`, `.raw`, `.vmdk` (satu file), `.vhd`, `.vhdx`, `.vdi`, dan `.iso`.
  Selain qcow2 dan iso, file otomatis dikonversi ke qcow2. File `.zip` harus di-unzip dulu.
- `.iso` (misalnya VyOS) disimpan sebagai `cdrom.iso` (read-only), ditambah disk kosong `disk.qcow2`
  (default 8 GB, bisa diatur saat upload). VM boot dari disk dulu, dan kalau disk masih kosong, boot dari CD.
  Jalankan installernya (VyOS: `install image`), maka boot berikutnya dari disk. **Wipe** mengosongkan disk node lagi.
- Disk yang merujuk ke file lain (backing file atau external data file) ditolak.
- Upload ditolak kalau sisa disk server kurang dari 2 GB (`NETLAB_DISK_RESERVE_MB`).
- Hanya satu upload yang bisa berjalan dalam satu waktu.
- Di dialog yang sama ada daftar image beserta tombol Delete. Image yang masih dipakai node di sebuah lab
  tidak bisa dihapus.

Cara manual dari server:

    mkdir -p data/images/qemu/vyos && cp vyos.qcow2 data/images/qemu/vyos/

Opsional `data/images/qemu/<nama>/netlab.json`:

    {"label": "VyOS 1.4", "category": "router", "nic": "virtio-net-pci", "disk_if": "virtio",
     "extra": [], "iface_prefix": "eth", "iface_start": 0}

`category` menentukan grup image di daftar **Device**: `router`, `switch`, `firewall`, atau `host`.
Tanpa category, image masuk grup Other. Image Cisco memakai `"nic": "e1000"`. Setiap node mendapat
overlay copy-on-write, jadi image dasarnya tidak ikut berubah. Konsolnya memakai serial
(telnet ke 127.0.0.1, dijembatani ke browser). Satu koneksi per VM dibagi ke semua tab yang membukanya, dan tab baru
langsung menampilkan output terakhir.

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

## AI assistant (Claude Code, Codex, OpenCode)

Tombol **AI assistant** di kanan atas membuka panel AI di sisi kanan, menggantikan panel Lab selama terbuka.
Pilih asistennya, lalu klik **Start on <lab>**. Asisten bekerja pada lab yang sedang dibuka, sedangkan console router
tetap di bawah. Lebar panel bisa diubah dengan menyeret tepi kirinya.
- **Claude Code**: login dengan akun Claude (Pro/Max) atau API key Anthropic.
- **Codex**: login dengan akun ChatGPT (Plus/Pro) lewat kode perangkat. Buka `https://auth.openai.com/codex/device`
  lalu masukkan kodenya. Kalau ditolak, aktifkan dulu *device code authorization* untuk Codex di pengaturan
  keamanan ChatGPT.
- **OpenCode** (open source): bisa langsung dipakai dengan model gratis. Ketik `/connect` atau klik **Sign in**
  untuk memakai Claude, ChatGPT, GitHub Copilot, dan provider lain.

Login disimpan di `data/ai/home/<provider>`, terpisah dari login Claude/Codex lain yang ada di server, dan
dipakai bersama oleh semua user NetLab. Asisten mengakses lab lewat `nlctl`, yang bisa:
- membaca topologi (`nlctl lab`);
- menjalankan perintah di node (`nlctl run R1 "show ip route"`): vtysh/shell untuk container, serial console untuk VM;
- menjalankan atau mematikan node.

Sebelum menjalankan perintah, asisten meminta izin, kecuali kamu mengizinkannya. `nlctl` mengakses lab lewat API server
NetLab di 127.0.0.1. Karena itu Codex tetap berjalan di sandbox-nya sendiri dan hanya diberi akses jaringan,
supaya bisa menghubungi server NetLab.

Console VM dibagi: tab console kamu dan asisten memakai console yang sama sekaligus, dan yang diketik asisten
terlihat di tab kamu. Saat asisten mengakses sebuah node, console node itu otomatis terbuka di bawah dengan
label "AI working", dan node-nya diberi garis oranye di lembar lab. Untuk router FRR/PC, tab "AI commands"
menampilkan perintah dan output-nya. Asisten mengakses node satu per satu: perintah berikutnya menunggu sampai
perintah sebelumnya selesai. Setiap asisten butuh RAM bebas sekitar 400 MB (`NETLAB_AI_MIN_RAM_MB`). Asisten berjalan sebagai root di server,
sama seperti NetLab sendiri.

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
