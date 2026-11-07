"""精排微调：远端训练客户端与任务同步。

数据在本地（MySQL），算力在远端（192.168.6.23 的 docker），所以这一层做
两件事：把训练请求发过去，再把远端状态同步回本地 `finetune_jobs`。
"""

from __future__ import annotations