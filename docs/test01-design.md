# TEST-01 测试隔离设计

用户已授权按既定顺序推进；本阶段目标和四项验收依据本会话任务卡。
基线：远端 a29af0c3fbcbbadca3a5159521a0884f35f25ad7；662bbe2 未推送，不伪造该提交。
范围：测试与测试夹具、测试文档。生产配置、预算、授权、UI 不变。
采用现有 pytest + 真实 harness（89fa546ea7a30cbeefb0f6292b928bd2cb01ce26）+ 本地 HTTP mock。
临时 env_file 显式绑定 HARNESS_MIMO_BASE_URL 和假 Key；父进程和 Python 子进程默认禁止非回环 DNS/连接。
不采用仅改环境变量名：无法兜住未来接线错误；也不采用 OS 沙箱：超出本阶段。
验收：污染配置不影响 mock；mock 关闭后失败且不回退；实际 mock 请求与真实文件同时存在；干净副本结果一致。
限制：Python socket 保护是测试防误调用，不是对抗恶意进程的 OS 沙箱。未知外部 harness 版本必须明确失败或跳过，不假报验证。
