"""种子记忆迁移脚本：把本机已有真实经验迁入 ~/xinhuo（一次性，保留作迁移记录）。

置信度说明：以下条目均在真实工作中验证过，迁移时直接置 verified，
last_verified 为迁移日；helpful 统计从零开始积累。
运行：.venv/bin/python scripts/seed.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xinhuo import config, gitops, index, store  # noqa: E402

TODAY = "2026-09-17"
SRC_Z = f"agent:zcode:migrated-{TODAY}"
SRC_H = "human:文剑"


def meta(dtype, title, scope, tags, source, relations=None, evidence=None):
    return {
        "id": "", "type": dtype, "title": title, "scope": scope, "tags": tags,
        "source": source, "evidence": evidence or [],
        "confidence": "verified", "status": "active", "last_verified": TODAY,
        "helpful_count": 0, "unhelpful_count": 0,
        "created": TODAY, "updated": TODAY, "relations": relations or [],
    }


SEEDS = [
    ("pitfall", meta("pitfall", "EasyConnect 7.6.7 在 macOS 26.1 上启动即 Rosetta 死锁", "global",
                     ["network", "vpn", "macos"], SRC_Z,
                     evidence=[{"type": "command", "value": "netstat -rn | grep ^172"}]),
     """## 现象
CSClient 启动即 Rosetta 死锁，重启重登均无效。

## 原因
7.6.7 版本客户端与 macOS 26.1 的 Rosetta 转译存在冲突。

## 正确做法
升级客户端版本。验证连通性看 `netstat -rn | grep ^172` 有无路由；ping 不通不代表断链。
"""),
    ("pitfall", meta("pitfall", "HAR 分析判断压缩只看 200 响应的 Content-Encoding，谨防 304 误判", "global",
                     ["har", "performance", "debugging"], SRC_Z,
                     relations=[{"id": "P-2026-0006", "type": "related"}]),
     """## 现象
分析 HAR 时看到大量 304 响应带 Content-Encoding，误以为压缩未开启或已开启。

## 原因
304 是缓存再验证响应，头是历史回显；content.size 恒为解压后大小，不能作为压缩证据。

## 正确做法
只看 200 响应的 Content-Encoding 判断压缩是否开启。另注意 no-cache 策略下带哈希的静态资源每次都会 304 再验证，属可优化项。
"""),
    ("pitfall", meta("pitfall", "本机渲染 xlsx 验收：无 soffice 时走 Numbers→PDF→PNG，日期格式必须小写", "global",
                     ["xlsx", "render", "macos"], SRC_Z),
     """## 现象
本机无 soffice，Word/Excel AppleScript 全部不可用，xlsx 无法直接渲染验收。

## 原因
环境缺 LibreOffice；且 Excel 数字格式串大小写敏感。

## 正确做法
走 Numbers→PDF→PNG 渲染链验收；日期格式必须用小写 yyyy-mm-dd，大写 YYYY-MM-DD 会产出错误结果。
"""),
    ("pitfall", meta("pitfall", "多仓库聚合项目禁止在根目录执行 git 命令", "global",
                     ["git", "multi-repo"], SRC_H),
     """## 现象
在聚合项目根目录跑 git 报 fatal: not a git repository，误判为"没有版本控制"。

## 原因
根目录不是 git 仓库，每个子目录才是独立仓库（各自有 .git / remote / 分支）。

## 正确做法
git 操作必须进对应子目录执行；典型如 cmi-sales-assistant 的四个子仓库，默认动作是改完代码在子目录内 add→commit→push origin main。
"""),
    ("pitfall", meta("pitfall", "Docker Hub 直连超时：用 DaoCloud 镜像加速拉取", "global",
                     ["docker", "registry"], SRC_H),
     """## 现象
docker pull 直连 Docker Hub 长时间无响应或超时。

## 原因
国内网络环境 Docker Hub 不可直连。

## 正确做法
~/.docker/daemon.json 配置 registry-mirrors 为 https://docker.m.daocloud.io 并重启 Docker 生效；基础镜像优先选多架构官方镜像。
"""),
    ("pitfall", meta("pitfall", "ncoa 门户加载慢的主因：后端冷查询慢与静态资源 no-cache 再验证", "cmi",
                     ["performance", "har", "frontend"], SRC_Z,
                     relations=[{"id": "P-2026-0002", "type": "related"}]),
     """## 现象
门户 onLoad 仅 3.17s，但数据齐全需约 12.5s。

## 原因
后端待办查询接口首查 wait 470~2427ms、二次调用仅 15~46ms，冷热差 50 倍是主因；三层串行链路与 2.25MB 主包解析次之；静态资源全部 no-cache，17 个哈希文件每次加载都发 304 再验证。

## 正确做法
优化后端缓存/预热；哈希静态资源改 max-age=31536000, immutable。注意 gzip 实际已开启，勿再提开压缩建议。
"""),
    ("workflow", meta("workflow", "CMI 日报生成流程：汇总四子仓库 git 提交、业务价值导向写作", "cmi",
                      ["daily-report"], SRC_H),
     """## 步骤
1. 对 4 个子仓库并行 git log（昨日 18:00 至今，--no-merges）
2. 按业务主题归类（不按仓库分）
3. 技术术语转译为业务价值语言（如 shadow→灰度验证）
4. 三段式结构：今日进展 / 风险协调 / 下一步计划
5. 每条以「完成 XX，保障/提升 YY」收尾，价值部分加粗
6. 写入日记目录；绝不自动 commit（用户手动提交，此规则优先于知识库自动提交规则）

## 产出
daily-YYYY-MM-DD.md
"""),
    ("workflow", meta("workflow", "文档类交付物渲染验收流水线（xlsx/docx/pdf）", "global",
                      ["render", "verify"], SRC_Z),
     """## 步骤
1. 生成源文件（xlsx 用 openpyxl 等库，不用手写 XML）
2. 渲染：soffice 优先；缺失时 Numbers→PDF→PNG
3. 逐页目检渲染 PNG（中文字体、日期小写 yyyy-mm-dd、列宽）
4. 公式/数据校验通过后再交付

## 产出
渲染 PNG + 验收结论
"""),
    ("preference", meta("preference", "未经点名的持久性设施（容器/守护）先征求同意再部署", "global",
                        ["deploy", "safety"], SRC_H),
     """## 规则
用户未点名的持久性设施（容器、守护进程、后台服务）不自主部署，先征求同意。

## 说明
变更后主动给出三清单：加了什么、没碰什么、如何清除。
"""),
    ("preference", meta("preference", "登记表格类请求默认 Excel 且先在对话里给字段清单", "global",
                        ["xlsx", "requirement"], SRC_H),
     """## 规则
用户说"登记表格"指 Excel 台账而非 Word；设计类请求先在对话里给出字段字典再生成文件，不要直接跑"生成+验收"全流水线。

## 说明
直接跑全流程会被嫌"太乱"；先对齐字段，文件是第二步。
"""),
    ("decision", meta("decision", "业务知识图谱五项设计决策（分层模型/LPG/DSL 确定性执行/YAML 治理/不做独立图服务）", "cmi",
                      ["kg", "architecture"], SRC_H),
     """## 背景
客户/欠费/合同域业务知识图谱的表示模型选型，多轮讨论收敛。

## 决策
1. schema+instance 混合分层（T-box 仅人改，A-box 可机器写）；
2. 元模型用 LPG 属性图；
3. 关系查询用 JSON 路径 DSL + 确定性执行器（LLM 只翻译不执行）；
4. 载体 YAML 源 + JSON 产物，lint 校验前移；
5. 不做独立图服务，引擎内嵌、资产=数据文件开放。

## 取舍
独立服务在千级节点规模下不成比例；出现多平台并发写/图过大/中心审计需求时再升级。
"""),
]


def main():
    rp = config.repo_path()
    id_by_pos = {}
    n = 0
    # 先按类型分配 id（与 save 的锁内分配一致）
    assigns = []
    for dtype, m, body in SEEDS:
        mid = store.alloc_id(rp, dtype)
        m["id"] = mid
        path = store.mem_path(rp, dtype, mid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(store.render(m, body), encoding="utf-8")
        assigns.append((m, body, path))
        id_by_pos[len(assigns) - 1] = mid
        n += 1
    # relations 里的 id 在分配后已引用正确（编号按类型独立递增，P-2026-0006 已在分配表内）
    conn = index.connect(rp)
    for m, body, path in assigns:
        index.upsert(conn, m, body, path, False)
    conn.close()
    index.build_index_md(rp)
    gitops.commit_all(rp, f"seed: 迁移 {n} 条真实经验（verified，源：内置记忆/AGENTS.md）")
    print(f"✓ 已写入 {n} 条种子记忆")
    for i, (m, _b, p) in enumerate(assigns):
        print(f"  {m['id']}  {m['title'][:44]}  ->  {p.name}")


if __name__ == "__main__":
    main()
