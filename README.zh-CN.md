# Hellbender Viewer

一个在自己电脑上运行的只读桌面小程序（Python/Tk），用来看你自己在密苏里大学 **Hellbender** 集群上的 Slurm 作业。点一次刷新就能看到：

- 运行中和排队中的作业，排队作业带 **Slurm 调度器给出的预计开始时间**；
- **此刻空闲、而且放得下你常用 GPU 作业的显卡**（按分区和型号）；
- 你的 **公平分（fairshare）** 和它大概怎样回升。

它不会改动集群上的任何东西，只在你点“刷新”时才查询。非官方工具，与 MU 研究计算支持部门无关。English: [README.md](README.md)。

![作业页（演示数据）](docs/screenshots/jobs.png)

## 界面

- 顶部三张卡片：**空闲 GPU**（gpu 公共分区，关注的型号如 L40S 排最前；底行是可被抢占的 gpu_requeue）、**公平分**（当前值、LevelFS、预计何时回到份额线）、**我的作业**（运行/排队数、最早预计开始、近 24 小时结束情况）。点卡片跳到对应页。
- **作业**页：活动 / 运行 / 排队 / 已结束 / 全部 过滤和搜索；右侧详情显示预计开始（没有估计时给出你在该分区的历史中位等待）、已用时间与时限、同类已完成作业的中位用时、资源和工作目录（可复制）。
- **空闲资源**页：每个 GPU 分区 × 型号的空闲/总数、“能立即用”的张数和所在节点；你关注型号在历次刷新时的空闲记录；CPU 分区里能立即放下你常用 CPU 作业的节点数。
- **公平分**页：当前值和解释、历次读数（实线）与预计走势（虚线），推算前提写在旁边。
- 设置：SSH 别名、外观（Neon / Midnight / Daylight）、关注的 GPU 型号、常用作业规格（留空按最近作业自动推断）、置顶、最小化到托盘、导出数据、退出。点窗口的 X 是收进托盘，退出用托盘菜单或 设置 → 退出程序。

## 它怎样访问集群

只有你点“刷新”（或按 F5）时：一次 SSH 会话，跑固定的只读命令，每条单独限时——`squeue`（你的作业）、`sinfo`（节点与 GPU 占用）、`sshare`（你的公平分，最多 30 分钟一次）、`scontrol show config` 的 Priority 行（最多 7 天一次）、`sacct`（你的记账，增量，最多 15 分钟一次）。两次刷新至少隔 5 分钟，失败会退避，限速状态重启后仍有效；启动、隐藏或空闲时不发任何请求；不读取集群上的文件，不修改任何作业或节点。详见 [COMMAND_POLICY.md](COMMAND_POLICY.md)。

因为记账是增量同步的，两次刷新之间开始又结束的作业也会被记下，带最终状态和退出码；从队列消失但还没查到记账的作业显示“结果未知”，不会被当成完成。

## 需要什么

- Python 3.10+（带 Tk），运行时只用标准库。
- OpenSSH 客户端，以及一个**不用交互就能登录**的 SSH 别名（密钥放在 ssh-agent 里）。Windows 上程序固定调用系统自带的 `C:\Windows\System32\OpenSSH\ssh.exe`，它能用 Windows 的 ssh-agent 服务。
- 在 Windows 11 上测试过；Linux/macOS 的图形界面没有验证过。

`~/.ssh/config` 示例（登录主机名见 Hellbender 官方文档）：

```text
Host hellbender
    HostName <Hellbender 登录主机>
    User <你的用户名>
    IdentityFile ~/.ssh/id_ed25519
```

## 运行

```text
git clone https://github.com/iserlohn0522-cell/hellbender-viewer-public
cd hellbender-viewer-public
python hellbender_viewer_v2.pyw --demo   # 离线演示，合成数据
python hellbender_viewer_v2.pyw          # 实际使用，默认 SSH 别名 hellbender（在 ⚙ 设置里改）
```

Windows 上 `Launch.vbs` / `Demo.vbs` 启动时不弹控制台；也可以建桌面快捷方式，目标为 `pythonw.exe "<文件夹>\hellbender_viewer_v2.pyw"`。

## 自己构建

```text
python -m pip install -r requirements-dev.txt         # 只有测试和截图需要 pytest、Pillow
python -m pytest tests -q
python tests/ui_check.py screenshots Neon Midnight Daylight   # 演示数据截图，不联网
python build.py                                        # 生成 dist/HellbenderViewer-<版本>.pyz
python dist/HellbenderViewer-3.0.0.pyz --demo
```

`.pyz` 是可携带的 zipapp，运行它的机器仍需要带 Tk 的 Python 和 OpenSSH。

改成自己的版本：SSH 别名、关注的 GPU 型号、常用作业规格都在设置里；集群时区是 `hellbender/insights.py` 里的 `CLUSTER_TZ`（Hellbender 为 America/Chicago）；名字里含 `gpu` 的分区被当作 GPU 分区。

## 数字的含义

- **空闲 GPU**：已配置但没被分配、且所在节点接受新作业的卡。**能立即用**还要求同一节点剩下的 CPU 和内存够你常用的 GPU 作业。被调度器预留给排队作业的节点、维护或下线的节点单独计数。这些都不保证马上开始（优先级、预留和 QOS 上限仍起作用），gpu_requeue 上的作业可能被抢占后重新排队。
- **公平分**：Slurm 给你账户关联的因子。在 Fair Tree 下它是你在所有用户中的排名（0–1，越高越优先），本身不做推算；推算的是 LevelFS（份额 ÷ 用量，和同组相比）：用量按集群的 `PriorityDecayHalfLife` 衰减，运行中的作业按跑满时限计，其他用户用量视为不变。只是估算。
- **预计开始**：Slurm 调度器自己的估计，换算成本机时间，会随队列变化。

## 本地数据

都存在本机 SQLite：Windows 为 `%LOCALAPPDATA%\HellbenderViewer\monitor.sqlite3`（macOS 为 `~/Library/Application Support/HellbenderViewer`，Linux 为 `$XDG_DATA_HOME/hellbender-viewer`）；演示模式用单独的 `demo.sqlite3`。不上传任何数据。

## 许可

MIT，见 [LICENSE](LICENSE)。
