#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Oracle Cloud 免费 ARM 实例「抢机」脚本（本地版）

思路参考社区项目：
  - hitrov/oci-arm-host-capacity（PHP，最老牌）
  - alexpua/oci-arm-catcher（macOS/Linux/Windows 单文件）
  - SUSANxBABU/Auto-Create-Oracle-Always-Free-ARM-VM-with-Telegram-Notification
共同原理：用 OCI API 反复调用 LaunchInstance，遇到
"Out of host capacity" / 500 InternalError 就退避重试，直到抢到容量为止。

用法：
  python3 grab.py check                 # 预检：可用域、子网、镜像、现有实例
  python3 grab.py grab --ocpus 2 --gbs 12   # 开始抢（默认 60 秒一轮）
  python3 grab.py grab --once           # 只试一次

依赖：pip install oci   （已装在 ~/oracle-grab/.venv）
凭据：~/.oci/config（由控制台「API 密钥」生成）
"""

import argparse
import json
import logging
import os
import random
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

import oci

# 本机 Clash 代理端口常常没开，而环境变量里残留 http_proxy 会让请求直接失败。
# 默认强制直连；如需走代理：GRAB_KEEP_PROXY=1 python3 grab.py ...
if not os.environ.get("GRAB_KEEP_PROXY"):
    for _k in ("http_proxy", "https_proxy", "all_proxy",
               "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(_k, None)

HOME = os.path.expanduser("~")
WORKDIR = os.path.join(HOME, "oracle-grab")
LOG_PATH = os.path.join(WORKDIR, "grab.log")
SF = "VM.Standard.A1.Flex"          # 免费 ARM（Ampere A1）
AMD_SF = "VM.Standard.E2.1.Micro"   # 免费 AMD 小机

log = logging.getLogger("grab")
NO_NOTIFY = False  # GitHub Actions 环境里关掉 macOS 弹窗/语音


def setup_logging():
    os.makedirs(WORKDIR, exist_ok=True)
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)


def notify(title: str, msg: str):
    """macOS 桌面通知 + 语音提示（CI 环境自动跳过）"""
    if NO_NOTIFY or os.environ.get("NO_NOTIFY"):
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{msg}" with title "{title}" sound name "Glass"'],
            check=False, timeout=10,
        )
    except Exception:
        pass
    try:
        subprocess.run(["say", "-v", "Tingting", msg], check=False, timeout=30)
    except Exception:
        pass


def load_config(path=None):
    path = os.path.expanduser(path or os.path.join(HOME, ".oci", "config"))
    if not os.path.exists(path):
        sys.exit(f"[!] 找不到 {path}\n    请先在 Oracle 控制台生成 API 密钥，或把配置放到这里。")
    cfg = oci.config.from_file(path)
    oci.config.validate_config(cfg)
    # 私钥路径若是相对路径，按配置文件目录解析
    kf = cfg.get("key_file")
    if kf and not os.path.isabs(kf):
        cfg["key_file"] = os.path.join(os.path.dirname(path), kf)
    return cfg


def clients(cfg):
    return (
        oci.identity.IdentityClient(cfg),
        oci.core.ComputeClient(cfg),
        oci.core.VirtualNetworkClient(cfg),
    )


def retry_call(fn, attempts=8, delay=6.0, what=""):
    """OCI 边缘节点偶发 401（新密钥同步/多 POP 不一致），这里统一重试"""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except oci.exceptions.ServiceError as e:
            last = e
            if e.status in (401, 429, 500, 502, 503, 504):
                log.info("%s 返回 %s，%.0f 秒后重试（%d/%d）", what or "接口", e.status, delay, i + 1, attempts)
                time.sleep(delay)
                continue
            raise
        except Exception as e:  # 网络抖动
            last = e
            log.info("%s 异常 %s，%.0f 秒后重试", what or "接口", e, delay)
            time.sleep(delay)
    raise last


def pick_availability_domain(identity, tenancy, want=None):
    ads = [a.name for a in retry_call(lambda: identity.list_availability_domains(tenancy), what="可用域").data]
    if not ads:
        sys.exit("[!] 该区域没有可用域，配置里的 region 可能不对")
    if want and want in ads:
        return want
    return ads[0]


def pick_subnet(network, compartment, ad, want=None):
    subs = [s for s in retry_call(lambda: network.list_subnets(compartment), what="子网").data
            if s.lifecycle_state == "AVAILABLE"]
    if want:
        for s in subs:
            if s.id == want or s.display_name == want:
                return s
        sys.exit(f"[!] 找不到子网 {want}")
    # 优先选区域级子网（availability_domain 为空）或同 AD 的子网
    cands = [s for s in subs if s.availability_domain in (None, "", ad)]
    if not cands:
        cands = subs
    if not cands:
        sys.exit("[!] 该 compartment 下没有可用子网，需要先在控制台建 VCN/子网")
    return cands[0]


def pick_image(compute, compartment, shape, want=None):
    if want:
        return want
    imgs = retry_call(lambda: compute.list_images(
        compartment,
        operating_system="Canonical Ubuntu",
        shape=shape,
        sort_by="TIMECREATED",
        sort_order="DESC",
    ), what="镜像").data
    if not imgs:
        imgs = retry_call(lambda: compute.list_images(compartment, shape=shape,
                                   sort_by="TIMECREATED", sort_order="DESC"), what="镜像").data
    if not imgs:
        sys.exit("[!] 没找到适配该 shape 的镜像，可用 --image 指定 OCID")
    return imgs[0].id


def ssh_pubkey(path=None):
    pub = os.path.join(WORKDIR, "id_ed25519.pub")
    if path:
        return open(os.path.expanduser(path), encoding="utf-8").read().strip()
    if not os.path.exists(pub):
        key = os.path.join(WORKDIR, "id_ed25519")
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "oracle-grab", "-f", key],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("已生成 SSH 密钥：%s", key)
    return open(pub, encoding="utf-8").read().strip()


def list_instances(compute, compartment):
    return [i for i in retry_call(lambda: compute.list_instances(compartment), what="实例列表").data
            if i.lifecycle_state not in ("TERMINATED", "TERMINATING")]


def free_quota_used(instances):
    """粗算已占用的 A1 免费额度（4 OCPU / 24GB 全区域合计）"""
    ocpu = mem = 0.0
    for i in instances:
        if i.shape == SF and i.shape_config:
            ocpu += float(i.shape_config.ocpus or 0)
            mem += float(i.shape_config.memory_in_gbs or 0)
    return ocpu, mem


def is_capacity_error(e):
    """是否是「没容量」这类可以继续重试的错误"""
    if isinstance(e, oci.exceptions.ServiceError):
        if e.status in (401, 429, 500, 502, 503, 504):
            return True
        code = (e.code or "") + " " + (e.message or "")
        for kw in ("OutOfHostCapacity", "Out of host capacity", "InternalError",
                   "Out of capacity", "TooManyRequests"):
            if kw.lower() in code.lower():
                return True
    return False


def cmd_check(args):
    cfg = load_config(args.config)
    identity, compute, network = clients(cfg)
    ten = cfg["tenancy"]
    log.info("region=%s  tenancy=%s", cfg["region"], ten)

    ads = [a.name for a in identity.list_availability_domains(ten).data]
    log.info("可用域：%s", ", ".join(ads))

    comp = args.compartment or ten
    insts = list_instances(compute, comp)
    if insts:
        for i in insts:
            log.info("现有实例：%s  %s  %s  %s OCPU/%s GB", i.display_name, i.shape,
                     i.lifecycle_state,
                     getattr(i.shape_config, "ocpus", "-"),
                     getattr(i.shape_config, "memory_in_gbs", "-"))
    else:
        log.info("现有实例：无（之前那台确实被回收了）")
    o, m = free_quota_used(insts)
    log.info("A1 已占用额度：%.1f OCPU / %.1f GB（免费上限 4 / 24）", o, m)

    sub = pick_subnet(network, comp, args.ad or ads[0], args.subnet)
    log.info("子网：%s  (%s)  vcn=%s", sub.display_name, sub.id, sub.vcn_id)
    img = pick_image(compute, comp, args.shape, args.image)
    log.info("镜像：%s", img)
    log.info("检查通过，可以开始抢机。")
    return 0


def cmd_grab(args):
    cfg = load_config(args.config)
    identity, compute, network = clients(cfg)
    ten = cfg["tenancy"]
    comp = args.compartment or ten

    ad = pick_availability_domain(identity, ten, args.ad)
    sub = pick_subnet(network, comp, ad, args.subnet)
    image_id = pick_image(compute, comp, args.shape, args.image)
    pubkey = ssh_pubkey(args.ssh_pubkey)


    if not args.allow_multiple and args.shape == SF:
        try:
            alive = [i for i in list_instances(compute, comp) if i.shape == SF]
        except Exception:
            alive = []
        if alive:
            for i in alive:
                log.info("已存在 A1 实例：%s  状态=%s  id=%s", i.display_name,
                         i.lifecycle_state, i.id)
            log.info("不再重复创建（要强制再开一台请加 --allow-multiple）")
            return 0

    if args.shape == SF:
        try:
            used_o, used_m = free_quota_used(list_instances(compute, comp))
        except Exception as e:
            log.warning("读取现有实例失败（不影响抢机）：%s", e)
            used_o = used_m = 0.0
        log.info("A1 已占用 %.1f OCPU / %.1f GB；本次申请 %.1f OCPU / %.1f GB",
                 used_o, used_m, args.ocpus, args.gbs)

    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=ad,
        compartment_id=comp,
        shape=args.shape,
        display_name=args.name,
        source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=image_id),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=sub.id, assign_public_ip=True, display_name=args.name + "-vnic"),
        metadata={"ssh_authorized_keys": pubkey},
    )
    if args.shape == SF:
        details.shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=float(args.ocpus), memory_in_gbs=float(args.gbs))

    log.info("目标：%s  %s  %s  %s  AD=%s", args.name, args.shape, sub.display_name, cfg["region"], ad)
    log.info("开始抢机，间隔 %.0f 秒（Ctrl-C 可停）", args.interval)

    deadline = time.time() + args.hours * 3600
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = compute.launch_instance(details)
        except oci.exceptions.ServiceError as e:
            if is_capacity_error(e):
                tag = "鉴权同步中" if e.status == 401 else "没抢到"
                log.info("#%d %s：%s %s —— %.0f 秒后再试", attempt, tag, e.status,
                         (e.message or e.code or "").strip()[:120], args.interval)
                if time.time() > deadline:
                    notify("抢机失败", "超过时限仍未抢到容量")
                    log.error("超过 %.0f 小时时限，退出", args.hours)
                    return 2
                time.sleep(args.interval + random.uniform(0, args.jitter))
                continue
            if e.status == 400 and "LimitExceeded" in (e.code or ""):
                notify("抢机失败", "免费额度不足或被限制")
                log.error("额度/限制错误：%s %s", e.code, e.message)
                return 3
            notify("抢机出错", f"{e.status} {e.code}")
            log.error("致命错误：%s %s", e.code, e.message)
            return 4
        except Exception as e:  # 网络抖动等
            log.warning("#%d 网络/其他异常：%s", attempt, e)
            time.sleep(args.interval)
            continue
        break

    inst_id = resp.data.id
    log.info("启动请求已受理：%s", inst_id)
    log.info("等待实例进入 RUNNING …")
    inst = resp.data
    try:
        for _ in range(60):
            inst = compute.get_instance(inst_id).data
            if inst.lifecycle_state == "RUNNING":
                break
            if inst.lifecycle_state in ("TERMINATED", "TERMINATING"):
                log.error("实例被终止：%s", inst.lifecycle_state)
                break
            time.sleep(10)
    except Exception as e:
        log.warning("等待 RUNNING 出错：%s（实例可能仍在创建）", e)

    ip = "-"
    try:
        vnics = compute.list_vnics(comp, instance_id=inst_id).data
        for v in vnics:
            atts = compute.list_vnic_attachments(comp, instance_id=inst_id).data
            for a in atts:
                if a.vnic_id == v.id:
                    vnic = network.get_vnic(v.id).data
                    if vnic.public_ip:
                        ip = vnic.public_ip
                        break
            if ip != "-":
                break
    except Exception as e:
        log.warning("取公网 IP 失败：%s", e)

    log.info("=" * 60)
    log.info("抢到了！实例：%s  状态：%s  公网 IP：%s", inst.display_name, inst.lifecycle_state, ip)
    log.info("登录：ssh -i %s ubuntu@%s", os.path.join(WORKDIR, "id_ed25519"), ip)
    log.info("=" * 60)
    try:
        with open(os.path.join(WORKDIR, "result.txt"), "w", encoding="utf-8") as f:
            f.write(f"实例：{inst.display_name}  状态：{inst.lifecycle_state}\n")
            f.write(f"规格：{args.shape} {args.ocpus}C / {args.gbs}G\n")
            f.write(f"区域：{cfg['region']}  可用域：{ad}\n")
            f.write(f"公网 IP：{ip}\n\n")
            f.write(f"ssh -i ~/oracle-grab/id_ed25519 ubuntu@{ip}\n")
    except Exception:
        pass
    notify("抢到甲骨文 ARM 服务器！", f"{inst.display_name}  IP {ip}")
    print(f"::notice title=抢到实例::{inst.display_name} {inst.lifecycle_state} IP={ip}", flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(f"## 抢到了 ✅\n\n- 实例：`{inst.display_name}` ({inst.lifecycle_state})\n"
                        f"- 规格：{args.shape} {args.ocpus}C/{args.gbs}G\n- 区域：{cfg['region']}\n"
                        f"- 公网 IP：`{ip}`\n- SSH：`ssh -i <你的私钥> ubuntu@{ip}`\n")
        except Exception:
            pass
    return 0


def main():
    p = argparse.ArgumentParser(description="Oracle 免费 ARM 实例抢机脚本")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--config", help="OCI 配置路径，默认 ~/.oci/config")
        sp.add_argument("--compartment", help="compartment OCID，默认根 tenancy")
        sp.add_argument("--ad", help="可用域名称")
        sp.add_argument("--subnet", help="子网 OCID 或名称")
        sp.add_argument("--image", help="镜像 OCID")
        sp.add_argument("--shape", default=SF, help=f"shape，默认 {SF}")

    c = sub.add_parser("check", help="预检")
    common(c)

    g = sub.add_parser("grab", help="开始抢")
    common(g)
    g.add_argument("--ocpus", type=float, default=2.0)
    g.add_argument("--gbs", type=float, default=12.0)
    g.add_argument("--name", default="free-arm")
    g.add_argument("--interval", type=float, default=60.0, help="重试间隔秒，默认 60")
    g.add_argument("--jitter", type=float, default=10.0)
    g.add_argument("--hours", type=float, default=48.0, help="最长运行小时数，默认 48")
    g.add_argument("--ssh-pubkey", help="公钥字符串文件路径，默认自动生成 ~/oracle-grab/id_ed25519")
    g.add_argument("--once", action="store_true", help="只尝试一次")
    g.add_argument("--allow-multiple", action="store_true", help="已有一台时仍继续再开")
    g.add_argument("--no-notify", action="store_true", help="不出 macOS 通知/语音（CI 用）")

    args = p.parse_args()
    global NO_NOTIFY
    NO_NOTIFY = bool(getattr(args, "no_notify", False))
    setup_logging()
    if args.cmd == "check":
        return cmd_check(args)
    if getattr(args, "once", False):
        args.hours = 0.0
    return cmd_grab(args)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *a: sys.exit(130))
    sys.exit(main())
