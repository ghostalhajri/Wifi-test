#!/usr/bin/env python3
"""
فاحص الوايفاي - Wi-Fi diagnostics
يشتغل على Windows / macOS / Linux بدون أي مكتبات إضافية (Python 3.8+).

الاستخدام:
    python wifi_diag.py              # فحص كامل
    python wifi_diag.py --no-scan    # بدون مسح الأجهزة في الشبكة
    python wifi_diag.py --no-speed   # بدون اختبار السرعة

يطلع التقرير على الشاشة ويحفظه في wifi_report.txt
"""
import argparse
import concurrent.futures as cf
import ipaddress
import locale
import platform
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from collections import Counter

OS = platform.system()  # 'Windows' / 'Darwin' / 'Linux'
REPORT = []
ISSUES = []  # (severity, message)  severity: 'high' / 'med' / 'low'

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def out(line=""):
    print(line)
    REPORT.append(line)


def issue(sev, msg):
    ISSUES.append((sev, msg))


def run(cmd, timeout=60):
    """يشغل أمر ويرجع المخرجات كنص (يتعامل مع ترميز ويندوز)."""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    raw = p.stdout + p.stderr
    for enc in ("utf-8", locale.getpreferredencoding(False), "cp850", "cp1256"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def pct_to_dbm(pct):
    return pct / 2 - 100


def band_of(channel):
    if channel is None:
        return None
    return "2.4GHz" if channel <= 14 else ("6GHz?" if channel > 177 else "5GHz")


# ---------------------------------------------------------------- Wi-Fi info

def wifi_windows():
    cur, nets = {}, []
    txt = run(["netsh", "wlan", "show", "interfaces"])
    for line in txt.splitlines():
        if ":" not in line:
            continue
        k, v = [s.strip() for s in line.split(":", 1)]
        kl = k.lower()
        if kl == "ssid":
            cur["ssid"] = v
        elif kl == "bssid":
            cur["bssid"] = v
        elif kl == "radio type":
            cur["radio"] = v
        elif kl == "channel":
            cur["channel"] = int(re.sub(r"\D", "", v) or 0) or None
        elif kl.startswith("receive rate"):
            cur["rx_rate"] = float(v.split()[0])
        elif kl.startswith("transmit rate"):
            cur["tx_rate"] = float(v.split()[0])
        elif kl == "signal":
            p = int(re.sub(r"\D", "", v) or 0)
            cur["signal_pct"] = p
            cur["signal_dbm"] = pct_to_dbm(p)
    txt = run(["netsh", "wlan", "show", "networks", "mode=bssid"])
    ssid, entry = None, None
    for line in txt.splitlines():
        s = line.strip()
        m = re.match(r"^SSID \d+\s*:\s*(.*)$", s)
        if m:
            ssid = m.group(1)
            continue
        if re.match(r"^BSSID \d+\s*:", s):
            entry = {"ssid": ssid}
            nets.append(entry)
            continue
        if entry is None or ":" not in s:
            continue
        k, v = [x.strip() for x in s.split(":", 1)]
        if k.lower() == "signal":
            entry["signal_dbm"] = pct_to_dbm(int(re.sub(r"\D", "", v) or 0))
        elif k.lower() == "channel":
            entry["channel"] = int(re.sub(r"\D", "", v) or 0) or None
    return cur, nets


def wifi_macos():
    cur, nets = {}, []
    txt = run(["system_profiler", "SPAirPortDataType"], timeout=90)
    section = None
    entry = None
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("Current Network Information:"):
            section = "cur"
            continue
        if s.startswith("Other Local Wi-Fi Networks:"):
            section = "other"
            continue
        if section in ("cur", "other") and s.endswith(":") and ":" not in s[:-1]:
            name = s[:-1]
            if section == "cur" and "ssid" not in cur:
                cur["ssid"] = name
                entry = cur
            else:
                section = "other"
                entry = {"ssid": name}
                nets.append(entry)
            continue
        if entry is None or ":" not in s:
            continue
        k, v = [x.strip() for x in s.split(":", 1)]
        if k == "Channel":
            m = re.match(r"(\d+)(?:\s*\(([^)]*)\))?", v)
            if m:
                entry["channel"] = int(m.group(1))
                if m.group(2):
                    w = re.search(r"(\d+)MHz", m.group(2))
                    if w:
                        entry["width"] = int(w.group(1))
        elif k == "Signal / Noise":
            m = re.findall(r"(-?\d+)\s*dBm", v)
            if m:
                entry["signal_dbm"] = int(m[0])
                if len(m) > 1:
                    entry["noise_dbm"] = int(m[1])
        elif k == "Transmit Rate":
            try:
                entry["tx_rate"] = float(v.split()[0])
            except ValueError:
                pass
        elif k == "PHY Mode":
            entry["radio"] = v
        elif k == "Interfaces" or k.startswith("Software Versions"):
            section = None
    nets = [n for n in nets if n is not cur]
    return cur, nets


def wifi_linux():
    cur, nets = {}, []
    # الشبكة الحالية عن طريق iw
    iface = None
    for line in run(["iw", "dev"]).splitlines():
        m = re.match(r"\s*Interface\s+(\S+)", line)
        if m:
            iface = m.group(1)
            break
    if iface:
        cur["iface"] = iface
        txt = run(["iw", "dev", iface, "link"])
        for line in txt.splitlines():
            s = line.strip()
            if s.startswith("SSID:"):
                cur["ssid"] = s[5:].strip()
            elif s.startswith("Connected to"):
                cur["bssid"] = s.split()[2]
            elif s.startswith("freq:"):
                f = float(s.split()[1])
                cur["channel"] = freq_to_channel(f)
            elif s.startswith("signal:"):
                cur["signal_dbm"] = int(float(s.split()[1]))
            elif s.startswith("tx bitrate:"):
                cur["tx_rate"] = float(s.split()[2])
                w = re.search(r"(\d+)MHz", s)
                if w:
                    cur["width"] = int(w.group(1))
                cur["radio"] = " ".join(
                    t for t in ("HE" in s and "Wi-Fi 6", "VHT" in s and "Wi-Fi 5",
                                "HT-MCS" in s and "Wi-Fi 4") if t) or None
            elif s.startswith("rx bitrate:"):
                cur["rx_rate"] = float(s.split()[2])
    # الشبكات المحيطة عن طريق nmcli
    txt = run(["nmcli", "-t", "-f", "IN-USE,SSID,BSSID,CHAN,SIGNAL", "dev", "wifi", "list", "--rescan", "yes"])
    for line in txt.splitlines():
        parts = [p.replace("\\:", ":") for p in re.split(r"(?<!\\):", line)]
        if len(parts) < 5:
            continue
        inuse, ssid, bssid, chan, sig = parts[:5]
        try:
            e = {"ssid": ssid, "bssid": bssid, "channel": int(chan),
                 "signal_dbm": pct_to_dbm(int(sig))}
        except ValueError:
            continue
        if inuse.strip() == "*":
            for k, v in e.items():
                cur.setdefault(k, v)
        else:
            nets.append(e)
    return cur, nets


def freq_to_channel(f):
    f = int(f)
    if 2412 <= f <= 2472:
        return (f - 2407) // 5
    if f == 2484:
        return 14
    if 5000 <= f <= 5900:
        return (f - 5000) // 5
    if 5925 <= f <= 7125:
        return (f - 5950) // 5
    return None


def analyze_wifi(cur, nets):
    out("== 1) اتصال الوايفاي ==")
    if not cur.get("ssid") and not cur.get("signal_dbm"):
        out("  ما قدرت أقرأ معلومات الوايفاي (يمكن الجهاز متصل بكيبل، أو يحتاج صلاحية الموقع).")
        if OS == "Windows":
            out("  ويندوز 11: فعّل Location من الإعدادات عشان netsh يقدر يشوف الشبكات.")
        out()
        return
    ch = cur.get("channel")
    band = band_of(ch)
    out(f"  الشبكة: {cur.get('ssid', '?')}   BSSID: {cur.get('bssid', '?')}")
    out(f"  القناة: {ch} ({band})   العرض: {cur.get('width', '?')} MHz   النوع: {cur.get('radio', '?')}")
    sig = cur.get("signal_dbm")
    if sig is not None:
        q = ("ممتازة" if sig >= -55 else "جيدة" if sig >= -67 else
             "ضعيفة" if sig >= -75 else "سيئة جداً")
        out(f"  قوة الإشارة: {sig:.0f} dBm ({q})")
        if sig < -75:
            issue("high", f"الإشارة سيئة جداً ({sig:.0f} dBm). قرّب من الراوتر، أو ارفع الراوتر عن الأرض وأبعده عن الجدران والمعادن، أو استخدم Mesh/مقوي.")
        elif sig < -67:
            issue("med", f"الإشارة ضعيفة ({sig:.0f} dBm). المكان بعيد عن الراوتر أو فيه عوائق.")
    if cur.get("noise_dbm") is not None and sig is not None:
        snr = sig - cur["noise_dbm"]
        out(f"  نسبة الإشارة للتشويش SNR: {snr} dB")
        if snr < 20:
            issue("high", f"التشويش عالي (SNR {snr} dB). فيه تداخل قوي: مايكرويف، أجهزة بلوتوث، أو شبكات جيران.")
    rate = cur.get("tx_rate") or cur.get("rx_rate")
    if rate:
        out(f"  سرعة الربط مع الراوتر: {rate:.0f} Mbps")
        if rate < 100:
            issue("med", f"سرعة الربط مع الراوتر منخفضة ({rate:.0f} Mbps). السبب غالباً ضعف الإشارة أو 2.4GHz أو جهاز قديم.")
    if band == "2.4GHz":
        issue("med", "جهازك متصل على 2.4GHz. هذا الحيز أبطأ وأكثر ازدحاماً. إذا الراوتر يدعم 5GHz اتصل عليه (أو افصل اسم الشبكتين في إعدادات الراوتر).")
    if cur.get("width") and band == "5GHz" and cur["width"] < 80:
        issue("low", f"عرض القناة {cur['width']}MHz على 5GHz. خلّه 80MHz من إعدادات الراوتر عشان تزيد السرعة.")

    out()
    out("== 2) ازدحام القنوات (شبكات الجيران) ==")
    if not nets:
        out("  ما قدرت أشوف الشبكات المحيطة.")
        out()
        return
    out(f"  عدد الشبكات المحيطة: {len(nets)}")
    by_ch = Counter(n.get("channel") for n in nets if n.get("channel"))
    out("  القنوات المستخدمة: " + ", ".join(f"{c}:{k}" for c, k in sorted(by_ch.items())))
    if ch:
        if band == "2.4GHz":
            overlap = [n for n in nets if n.get("channel") and n["channel"] <= 14 and abs(n["channel"] - ch) < 5]
            strong = [n for n in overlap if (n.get("signal_dbm") or -100) > -70]
            out(f"  شبكات تتداخل مع قناتك ({ch}): {len(overlap)}، منها {len(strong)} قوية")
            if ch not in (1, 6, 11):
                issue("med", f"قناتك {ch} على 2.4GHz تتداخل مع قناتين. استخدم 1 أو 6 أو 11 بس.")
            if len(strong) >= 3 or len(overlap) >= 6:
                best = min((1, 6, 11), key=lambda c: sum(1 for n in nets if n.get("channel") and n["channel"] <= 14 and abs(n["channel"] - c) < 5))
                issue("high", f"القناة {ch} زحمة ({len(overlap)} شبكة متداخلة). غيّرها في الراوتر للقناة {best} لأنها أقل وحدة مستخدمة.")
        else:
            same = [n for n in nets if n.get("channel") == ch]
            out(f"  شبكات على نفس قناتك ({ch}): {len(same)}")
            if len(same) >= 3:
                used = set(by_ch)
                free = [c for c in (36, 40, 44, 48, 149, 153, 157, 161) if c not in used]
                hint = f" مثلاً {free[0]}" if free else ""
                issue("med", f"قناة 5GHz رقم {ch} عليها {len(same)} شبكات ثانية. غيّرها لقناة فاضية{hint}.")
    out()


# ---------------------------------------------------------------- network

def default_gateway():
    if OS == "Windows":
        txt = run(["route", "print", "-4", "0.0.0.0"])
        m = re.search(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)", txt, re.M)
        return m.group(1) if m else None
    if OS == "Darwin":
        m = re.search(r"gateway:\s*(\S+)", run(["route", "-n", "get", "default"]))
        return m.group(1) if m else None
    m = re.search(r"default via (\S+)", run(["ip", "route", "show", "default"]))
    return m.group(1) if m else None


def ping(host, count):
    if OS == "Windows":
        cmd = ["ping", "-n", str(count), "-w", "1000", host]
    elif OS == "Darwin":
        cmd = ["ping", "-c", str(count), "-W", "1000", host]
    else:
        cmd = ["ping", "-c", str(count), "-i", "0.2", "-W", "1", host]
    txt = run(cmd, timeout=count * 2 + 10)
    times = [float(x) for x in re.findall(r"[=<]\s*([\d.]+)\s*ms", txt, re.I)]
    # Windows يكتب ملخص فيه أرقام ms - نشيل سطر الملخص
    if OS == "Windows":
        times = [float(x) for x in re.findall(r"(?:time|الوقت)\s*[=<]\s*([\d.]+)\s*ms", txt, re.I)] or times[:count]
    times = times[:count]
    loss = 100.0 * (count - len(times)) / count
    if not times:
        return {"loss": 100.0}
    jitter = statistics.mean(abs(a - b) for a, b in zip(times, times[1:])) if len(times) > 1 else 0
    return {"loss": loss, "avg": statistics.mean(times), "max": max(times), "jitter": jitter}


def fmt_ping(r):
    if "avg" not in r:
        return "ما رد (فقدان 100%)"
    return f"متوسط {r['avg']:.1f}ms | أعلى {r['max']:.0f}ms | تذبذب {r['jitter']:.1f}ms | فقدان {r['loss']:.0f}%"


def analyze_latency(gw):
    out("== 3) التأخير وفقدان الحزم ==")
    n = 20 if OS == "Linux" else 10
    g = None
    if gw:
        g = ping(gw, n)
        out(f"  إلى الراوتر ({gw}): {fmt_ping(g)}")
    else:
        out("  ما لقيت عنوان الراوتر (Gateway).")
    i = ping("1.1.1.1", n)
    out(f"  إلى الإنترنت (1.1.1.1): {fmt_ping(i)}")
    if g and "avg" in g:
        if g["loss"] >= 5:
            issue("high", f"فيه فقدان حزم {g['loss']:.0f}% بين جهازك والراوتر. المشكلة من الوايفاي نفسه (إشارة أو تداخل).")
        if g["avg"] > 20 or g["jitter"] > 15:
            issue("high", f"التأخير للراوتر عالي ({g['avg']:.0f}ms). المفروض يكون أقل من 5ms، وهذا يدل على تداخل أو ضغط على الراوتر.")
    if "avg" in i:
        local_ok = g and "avg" in g and g["loss"] < 5 and g["avg"] < 20
        if i["loss"] >= 3 and local_ok:
            issue("high", f"فيه فقدان {i['loss']:.0f}% للإنترنت والوايفاي سليم. المشكلة غالباً من مزوّد الخدمة أو خط الألياف/DSL.")
        if i["avg"] > 80 and local_ok:
            issue("med", f"التأخير للإنترنت عالي ({i['avg']:.0f}ms) والشبكة الداخلية سليمة. المشكلة من مزوّد الخدمة.")
    elif gw and g and "avg" in g:
        issue("high", "الراوتر يرد بس الإنترنت ما يرد. فيه مشكلة في خط الإنترنت أو عند المزوّد.")
    out()
    return g, i


def analyze_dns():
    out("== 4) سرعة DNS ==")
    times = []
    for host in ("google.com", "youtube.com", "netflix.com", "microsoft.com", "apple.com"):
        t = time.perf_counter()
        try:
            socket.getaddrinfo(host, 443)
            times.append((time.perf_counter() - t) * 1000)
        except socket.gaierror:
            pass
    if times:
        avg = statistics.mean(times)
        out(f"  متوسط وقت ترجمة الأسماء: {avg:.0f}ms (أبطأ: {max(times):.0f}ms)")
        if max(times) > 150:
            issue("low", "الـDNS بطيء، ويخلي فتح المواقع يتأخر. غيّر الـDNS في الراوتر إلى 1.1.1.1 و 8.8.8.8.")
    else:
        out("  فشل DNS بالكامل!")
        issue("high", "الـDNS ما يشتغل. غيّره في الراوتر إلى 1.1.1.1 / 8.8.8.8.")
    out()


# ---------------------------------------------------------------- speed / bufferbloat

UA = {"User-Agent": "Mozilla/5.0 wifi-diag"}


def download_test(seconds=10):
    url = "https://speed.cloudflare.com/__down?bytes=100000000"
    total, start = 0, time.perf_counter()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
            while time.perf_counter() - start < seconds:
                chunk = r.read(65536)
                if not chunk:
                    break
                total += len(chunk)
    except Exception as e:
        return None, str(e)
    dur = time.perf_counter() - start
    return total * 8 / dur / 1e6, None


def upload_test(seconds=8):
    url = "https://speed.cloudflare.com/__up"
    size = 10_000_000
    data = b"0" * size
    total, start = 0, time.perf_counter()
    try:
        while time.perf_counter() - start < seconds:
            req = urllib.request.Request(url, data=data, headers={**UA, "Content-Type": "application/octet-stream"})
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
            total += size
    except Exception as e:
        if total == 0:
            return None, str(e)
    dur = time.perf_counter() - start
    return total * 8 / dur / 1e6, None


def analyze_speed(idle_ping):
    out("== 5) السرعة و Bufferbloat (التأخير وقت التحميل) ==")
    loaded = {}

    def bg_ping():
        loaded.update(ping("1.1.1.1", 15 if OS == "Linux" else 8))

    t = threading.Thread(target=bg_ping)
    t.start()
    dl, err = download_test()
    t.join()
    if dl is None:
        out(f"  فشل اختبار التحميل: {err}")
        out()
        return
    out(f"  سرعة التحميل: {dl:.1f} Mbps")
    ul, err = upload_test()
    out(f"  سرعة الرفع:  {ul:.1f} Mbps" if ul else f"  فشل اختبار الرفع: {err}")
    if "avg" in loaded and "avg" in idle_ping:
        inc = loaded["avg"] - idle_ping["avg"]
        out(f"  التأخير وقت التحميل: {loaded['avg']:.0f}ms (زاد {inc:+.0f}ms عن الوضع العادي)")
        if inc > 100:
            issue("high", f"فيه Bufferbloat قوي (التأخير يزيد {inc:.0f}ms وقت التحميل). يسبب تقطيع في الألعاب والمكالمات لما أحد يحمّل. فعّل SQM أو Smart QoS في الراوتر.")
        elif inc > 40:
            issue("med", f"فيه Bufferbloat متوسط (+{inc:.0f}ms). إذا الراوتر فيه QoS/SQM فعّله.")
    out("  (قارن السرعة بالباقة اللي تدفع عليها. إذا أقل من 50% وأنت قريب من الراوتر، جرّب بكيبل عشان تعرف المشكلة من الوايفاي ولا من الخط.)")
    out()


# ---------------------------------------------------------------- devices

def ping_once(ip):
    if OS == "Windows":
        cmd = ["ping", "-n", "1", "-w", "300", ip]
    elif OS == "Darwin":
        cmd = ["ping", "-c", "1", "-W", "300", ip]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", ip]
    try:
        subprocess.run(cmd, capture_output=True, timeout=3)
    except Exception:
        pass


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def analyze_devices(gw, do_scan):
    out("== 6) الأجهزة المتصلة بالشبكة ==")
    me = local_ip()
    if not gw or not me:
        out("  ما قدرت أحدد الشبكة.")
        out()
        return
    net = ipaddress.ip_network(f"{me}/24", strict=False)
    if do_scan:
        out(f"  أمسح الشبكة {net} ...")
        with cf.ThreadPoolExecutor(64) as ex:
            list(ex.map(ping_once, [str(h) for h in net.hosts()]))
    txt = run(["ip", "neigh"]) if OS == "Linux" else run(["arp", "-a"])
    devs = {}
    for m in re.finditer(r"(\d+\.\d+\.\d+\.\d+)\D+?([0-9a-fA-F]{1,2}(?:[:-][0-9a-fA-F]{1,2}){5})", txt):
        ip, mac = m.group(1), m.group(2).lower().replace("-", ":")
        if ipaddress.ip_address(ip) in net and mac not in ("ff:ff:ff:ff:ff:ff",) and not ip.endswith(".255"):
            devs[ip] = mac
    out(f"  عدد الأجهزة اللي ردّت: {len(devs)} (غير جهازك)")
    for ip in sorted(devs, key=lambda x: ipaddress.ip_address(x)):
        mac = devs[ip]
        tag = "  <- الراوتر" if ip == gw else ""
        rnd = int(mac.split(":")[0], 16) & 2
        note = " (MAC عشوائي، غالباً جوال)" if rnd else ""
        out(f"    {ip:<16} {mac}{note}{tag}")
    if len(devs) > 25:
        issue("med", f"فيه {len(devs)} جهاز على الشبكة. كثرة الأجهزة تبطّي الراوتر، خصوصاً لو كان رخيص. تأكد إنك تعرف كل جهاز وافصل اللي ما تحتاجه.")
    out("  إذا فيه جهاز ما تعرفه: غيّر كلمة سر الوايفاي واستخدم WPA2/WPA3.")
    out()


def check_router_page(gw):
    if not gw:
        return
    for scheme in ("http", "https"):
        try:
            s = socket.create_connection((gw, 80 if scheme == "http" else 443), timeout=2)
            s.close()
            out(f"  صفحة إعدادات الراوتر: {scheme}://{gw}")
            return
        except OSError:
            continue


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-scan", action="store_true", help="بدون مسح الأجهزة")
    ap.add_argument("--no-speed", action="store_true", help="بدون اختبار السرعة")
    args = ap.parse_args()

    out("=" * 60)
    out(f" تقرير فحص الوايفاي  -  {time.strftime('%Y-%m-%d %H:%M')}  -  {OS}")
    out("=" * 60)
    out()

    try:
        cur, nets = {"Windows": wifi_windows, "Darwin": wifi_macos}.get(OS, wifi_linux)()
    except Exception as e:
        cur, nets = {}, []
        out(f"  خطأ في قراءة الوايفاي: {e}")
    analyze_wifi(cur, nets)

    gw = default_gateway()
    g, i = analyze_latency(gw)
    analyze_dns()
    if not args.no_speed:
        analyze_speed(i)
    analyze_devices(gw, not args.no_scan)

    out("=" * 60)
    out(" الخلاصة: اللي يبطّي الوايفاي عندك")
    out("=" * 60)
    if not ISSUES:
        out("  ما لقيت مشاكل واضحة. إذا للحين تحس ببطء، قارن السرعة بالباقة أو كلّم المزوّد.")
    order = {"high": 0, "med": 1, "low": 2}
    label = {"high": "[مهم]  ", "med": "[متوسط]", "low": "[بسيط] "}
    for n, (sev, msg) in enumerate(sorted(ISSUES, key=lambda x: order[x[0]]), 1):
        out(f"  {n}. {label[sev]} {msg}")
    out()
    out(" نصائح عامة من صفحة الراوتر:")
    check_router_page(gw)
    out("  - حدّث فيرموير الراوتر، وسوّ له إعادة تشغيل كل فترة.")
    out("  - خلّ الحماية WPA2-AES أو WPA3. TKIP/WEP يحدد السرعة بـ54Mbps.")
    out("  - طفّ الـ\"Legacy/802.11b mode\" إذا كان شغال.")
    out()

    with open("wifi_report.txt", "w", encoding="utf-8-sig") as f:
        f.write("\n".join(REPORT))
    print("تم حفظ التقرير في wifi_report.txt")


if __name__ == "__main__":
    main()
