# -*- coding: utf-8 -*-
"""tools.py —— 物联网感知层与业务数据层

两条数据通路，同一个接口：

1. **真实设备通路**：ESP32 等设备通过 MQTT 网关或直接 HTTP 上报到
   POST /api/ingest，数据写入 data/readings.json，带时间戳与设备标识。
2. **演示兜底通路**：未接入真实设备时，按真实物理量的合理范围做随机游走，
   保证界面有连续、可信的读数曲线可以演示。

智能体只调用统一的方法，不关心数据来自哪条通路——接入真实硬件时，
上层代码与提示词完全不用改。这一点在答辩时可以直接讲成"感知层可插拔"。
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

CST = timezone(timedelta(hours=8))
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# 读取超过该秒数视为"真实数据已过期"，回落到仿真数据
REAL_FRESH_SECONDS = 180

# 地块初始档案：作物按岭南地区学期内可完成一个周期来选
SEED_PLOTS = [
    {"plot_id": "P01", "name": "一号地", "crop": "菜心", "area_m2": 18,
     "group": "物联网工程2301班", "owner": "第三小组",
     "soil": 62.0, "temp": 24.5, "light": 38500},
    {"plot_id": "P02", "name": "二号地", "crop": "生菜", "area_m2": 16,
     "group": "物联网工程2301班", "owner": "第一小组",
     "soil": 48.0, "temp": 25.8, "light": 41200},
    {"plot_id": "P03", "name": "三号地", "crop": "番茄", "area_m2": 24,
     "group": "物联网工程2302班", "owner": "第五小组",
     "soil": 33.0, "temp": 27.2, "light": 45600},
    {"plot_id": "P04", "name": "四号地", "crop": "黄瓜", "area_m2": 20,
     "group": "物联网工程2302班", "owner": "第二小组",
     "soil": 78.0, "temp": 26.4, "light": 33100},
]

# 演示用的劳动打卡记录（真实系统应来自学校劳动教育管理平台）
SEED_LOGS = [
    {"date": "2026-09-18", "plot_id": "P03", "task": "整地做畦、施基肥",
     "hours": 3.0, "people": 5, "note": "深翻25cm，做高畦"},
    {"date": "2026-09-25", "plot_id": "P03", "task": "番茄育苗与定植",
     "hours": 2.5, "people": 5, "note": "定植42株，浇定根水"},
    {"date": "2026-10-09", "plot_id": "P03", "task": "搭架绑蔓、整枝打杈",
     "hours": 2.0, "people": 4, "note": "单干整枝，保留主蔓"},
    {"date": "2026-10-23", "plot_id": "P03", "task": "追肥、除草、病虫害巡查",
     "hours": 2.5, "people": 4, "note": "追施复合肥，发现少量蚜虫"},
    {"date": "2026-11-06", "plot_id": "P03", "task": "疏果、培土、滴灌检修",
     "hours": 2.0, "people": 4, "note": "每穗留4果，更换堵头2个"},
    {"date": "2026-11-20", "plot_id": "P03", "task": "采收与测产",
     "hours": 1.5, "people": 5, "note": "采收18.6千克，送食堂加工"},
]


@dataclass
class Reading:
    plot_id: str
    soil: float          # 土壤体积含水率 %
    temp: float          # 空气温度 ℃
    light: int           # 光照强度 lux
    battery: int         # 设备电量 %
    source: str          # "device" 真实设备 / "sim" 仿真
    ts: str              # ISO 时间戳

    def as_dict(self) -> dict:
        d = asdict(self)
        d["soil"] = round(self.soil, 1)
        d["temp"] = round(self.temp, 1)
        return d


class IoTStore:
    """地块档案 + 实时读数 + 劳动打卡记录"""

    def __init__(self, data_dir: str = DATA_DIR):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self.plots_path = os.path.join(self.data_dir, "plots.json")
        self.readings_path = os.path.join(self.data_dir, "readings.json")
        self.logs_path = os.path.join(self.data_dir, "practice_logs.json")
        self._rng = random.Random(20261123)
        self.plots: List[dict] = self._load(self.plots_path, SEED_PLOTS)
        self.logs: List[dict] = self._load(self.logs_path, SEED_LOGS)
        self.real: Dict[str, dict] = self._load(self.readings_path, {})
        self._state: Dict[str, Reading] = {}
        self._tick()

    # ---------- 持久化 ----------
    @staticmethod
    def _load(path: str, default):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, json.JSONDecodeError):
                pass
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(default, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
        return default

    def _save(self, path: str, obj) -> None:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(obj, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ---------- 设备上报 ----------
    def ingest(self, plot_id: str, soil: float | None, temp: float | None,
               light: int | None, battery: int | None = None,
               device: str = "esp32") -> Reading:
        """真实设备上报入口。缺项用上一刻读数补齐。"""
        prev = self._state.get(plot_id)
        now = datetime.now(CST).isoformat(timespec="seconds")
        reading = Reading(
            plot_id=plot_id,
            soil=float(soil) if soil is not None else (prev.soil if prev else 55.0),
            temp=float(temp) if temp is not None else (prev.temp if prev else 25.0),
            light=int(light) if light is not None else (prev.light if prev else 30000),
            battery=int(battery) if battery is not None else (prev.battery if prev else 100),
            source="device",
            ts=now,
        )
        self._state[plot_id] = reading
        self.real[plot_id] = {"reading": reading.as_dict(), "device": device,
                              "epoch": time.time()}
        self._save(self.readings_path, self.real)
        return reading

    # ---------- 读数获取 ----------
    def _sim(self, plot: dict) -> Reading:
        """以初始值为中心做有界随机游走，读起来像真实环境。"""
        prev = self._state.get(plot["plot_id"])
        if prev is None:
            soil, temp, light = plot["soil"], plot["temp"], plot["light"]
            battery = 92
        else:
            soil, temp, light, battery = prev.soil, prev.temp, prev.light, prev.battery

        soil = min(92.0, max(18.0, soil + self._rng.uniform(-1.6, 1.4)))
        temp = min(38.0, max(12.0, temp + self._rng.uniform(-0.45, 0.45)))
        light = min(88000, max(1200, light + self._rng.randint(-2600, 2600)))
        battery = max(15, battery - (1 if self._rng.random() < 0.02 else 0))
        return Reading(plot["plot_id"], soil, temp, light, battery, "sim",
                       datetime.now(CST).isoformat(timespec="seconds"))

    def _tick(self) -> None:
        for plot in self.plots:
            pid = plot["plot_id"]
            real = self.real.get(pid)
            if real and (time.time() - real.get("epoch", 0)) < REAL_FRESH_SECONDS:
                r = real["reading"]
                self._state[pid] = Reading(pid, float(r["soil"]), float(r["temp"]),
                                           int(r["light"]), int(r.get("battery", 100)),
                                           "device", str(r.get("ts", "")))
            else:
                self._state[pid] = self._sim(plot)

    # ---------- 智能体可调用的工具 ----------
    def list_plots(self) -> List[dict]:
        self._tick()
        out = []
        for p in self.plots:
            r = self._state[p["plot_id"]]
            advice = judge_reading(p["crop"], r)
            out.append({
                "plot_id": p["plot_id"], "name": p["name"], "crop": p["crop"],
                "area_m2": p["area_m2"], "group": p["group"], "owner": p["owner"],
                "soil": round(r.soil, 1), "temp": round(r.temp, 1),
                "light": r.light, "battery": r.battery, "source": r.source,
                "ts": r.ts,
                # 状态与告警由服务端统一判读，避免前端另设阈值造成前后不一致
                "status": advice["status"], "alerts": advice["alerts"],
            })
        return out

    def get_plot_data(self, plot_id: str) -> dict:
        self._tick()
        pid = (plot_id or "").strip().upper()
        plot = next((p for p in self.plots if p["plot_id"].upper() == pid), None)
        if plot is None:
            return {"ok": False, "error": f"未找到地块 {plot_id}",
                    "available": [p["plot_id"] for p in self.plots]}
        r = self._state[plot["plot_id"]]
        advice = judge_reading(plot["crop"], r)
        return {
            "ok": True,
            "plot_id": plot["plot_id"], "name": plot["name"], "crop": plot["crop"],
            "area_m2": plot["area_m2"], "owner": plot["owner"],
            "soil": round(r.soil, 1), "temp": round(r.temp, 1),
            "light": r.light, "battery": r.battery, "source": r.source, "ts": r.ts,
            "status": advice["status"], "alerts": advice["alerts"],
        }

    def get_practice_logs(self, plot_id: str | None = None,
                          days: int | None = None) -> List[dict]:
        rows = self.logs
        if plot_id:
            pid = plot_id.strip().upper()
            rows = [r for r in rows if str(r.get("plot_id", "")).upper() == pid]
        if days:
            cutoff = (datetime.now(CST) - timedelta(days=int(days))).date().isoformat()
            rows = [r for r in rows if str(r.get("date", "")) >= cutoff]
        return sorted(rows, key=lambda r: str(r.get("date", "")))

    def summarize_practice(self, plot_id: str | None = None) -> dict:
        rows = self.get_practice_logs(plot_id)
        total_h = sum(float(r.get("hours", 0)) for r in rows)
        total_p = sum(int(r.get("people", 0)) for r in rows)
        return {
            "times": len(rows),
            "total_hours": round(total_h, 1),
            "person_hours": round(total_h * (total_p / len(rows)), 1) if rows else 0,
            "first_date": rows[0]["date"] if rows else None,
            "last_date": rows[-1]["date"] if rows else None,
            "tasks": [r.get("task", "") for r in rows],
        }

    def get_weather(self, city: str = "广州") -> dict:
        """演示用气候参考；接入真实气象 API 时只需替换本方法。"""
        month = datetime.now(CST).month
        table = {
            1: ("晴到多云", 15, 62), 2: ("阴雨", 17, 70), 3: ("回南天", 21, 78),
            4: ("阵雨", 25, 75), 5: ("雷阵雨", 28, 72), 6: ("大到暴雨", 31, 74),
            7: ("雷雨", 33, 70), 8: ("台风季", 33, 71), 9: ("阵雨", 30, 68),
            10: ("晴", 27, 60), 11: ("晴到多云", 23, 58), 12: ("晴到多云", 18, 55),
        }
        desc, temp, hum = table.get(month, ("多云", 25, 65))
        return {"city": city, "month": month, "desc": desc, "temp_c": temp,
                "humidity": hum, "source": "seasonal_reference",
                "note": "按岭南各月常年气候特征给出的参考值"}

    def stats(self) -> dict:
        return {"plots": len(self.plots), "logs": len(self.logs),
                "real_devices": sum(
                    1 for v in self.real.values()
                    if time.time() - v.get("epoch", 0) < REAL_FRESH_SECONDS)}


# ---------- 读数判读：把数字翻译成可执行动作 ----------
THRESHOLDS = {
    "叶菜": {"soil": (55, 75), "temp": (15, 25)},
    "生菜": {"soil": (55, 75), "temp": (15, 25)},
    "菜心": {"soil": (55, 75), "temp": (15, 25)},
    "番茄": {"soil": (60, 80), "temp": (20, 28)},
    "辣椒": {"soil": (60, 80), "temp": (20, 28)},
    "茄子": {"soil": (60, 80), "temp": (20, 28)},
    "黄瓜": {"soil": (65, 85), "temp": (22, 30)},
    "丝瓜": {"soil": (65, 85), "temp": (22, 30)},
    "冬瓜": {"soil": (65, 85), "temp": (22, 30)},
    "萝卜": {"soil": (55, 70), "temp": (15, 25)},
}
DEFAULT_TH = {"soil": (55, 75), "temp": (15, 28)}


def judge_reading(crop: str, r: Reading) -> dict:
    """按作物适宜区间给出状态与告警，供智能体作为事实依据。"""
    th = THRESHOLDS.get(crop, DEFAULT_TH)
    lo, hi = th["soil"]
    tlo, thi = th["temp"]
    alerts: List[str] = []
    level = "正常"

    if r.soil < lo - 15:
        alerts.append(f"土壤湿度 {r.soil:.1f}% 严重偏低（{crop}适宜 {lo}–{hi}%），应立即灌溉")
        level = "告警"
    elif r.soil < lo:
        alerts.append(f"土壤湿度 {r.soil:.1f}% 略低于适宜区间（{lo}–{hi}%），建议傍晚补水")
        level = "注意" if level == "正常" else level
    elif r.soil > hi + 10:
        alerts.append(f"土壤湿度 {r.soil:.1f}% 明显偏高（{crop}适宜 {lo}–{hi}%），需排水通风，防烂根")
        level = "告警"
    elif r.soil > hi:
        alerts.append(f"土壤湿度 {r.soil:.1f}% 略高于适宜区间（{lo}–{hi}%），暂缓灌溉")
        level = "注意" if level == "正常" else level

    if r.temp > thi + 5:
        alerts.append(f"气温 {r.temp:.1f}℃ 偏高（适宜 {tlo}–{thi}℃），建议遮阳并早晚灌溉")
        level = "告警" if level != "告警" else level
    elif r.temp > thi:
        alerts.append(f"气温 {r.temp:.1f}℃ 略高于适宜区间（{tlo}–{thi}℃），注意通风")
        level = "注意" if level == "正常" else level
    elif r.temp < tlo:
        alerts.append(f"气温 {r.temp:.1f}℃ 低于适宜区间（{tlo}–{thi}℃），生长可能停滞")
        level = "注意" if level == "正常" else level

    if r.light < 8000:
        alerts.append(f"光照 {r.light} lux 偏弱，易徒长，建议调整位置或补光")
        level = "注意" if level == "正常" else level
    elif r.light > 80000:
        alerts.append(f"光照 {r.light} lux 过强，幼嫩叶片可能灼伤，建议遮阳")

    if r.battery < 25:
        alerts.append(f"设备电量 {r.battery}%，请及时更换电池，避免数据中断")

    if not alerts:
        alerts.append(f"土壤湿度、气温、光照均在{crop}适宜区间内，保持当前管理即可")
    return {"status": level, "alerts": alerts}


if __name__ == "__main__":
    store = IoTStore()
    print("工具层统计:", store.stats())
    for p in store.list_plots():
        print(f"  {p['plot_id']} {p['name']} {p['crop']:<3} "
              f"湿度{p['soil']:>5.1f}% 温度{p['temp']:>5.1f}℃ 光照{p['light']:>6}lx "
              f"[{p['source']}]")
    print()
    d = store.get_plot_data("P03")
    print("P03 判读:", d["status"])
    for a in d["alerts"]:
        print("   -", a)
    print()
    print("劳动汇总:", store.summarize_practice("P03"))
