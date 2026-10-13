/**
 * mockArtifacts — 成果库演示数据（带来源说明）
 *
 * 来源说明：以下条目为手工编写的演示内容，结构与文案改写自
 * 前端大框架/index.html 交互原型中的 artifacts 示例（a1/a2/a3），
 * 不来自任何后端接口或真实会话记录，仅供成果库对话框展示与导出演示。
 */

export type MockArtifactType = '笔记' | '代码变更' | '文档'

export interface MockArtifact {
  id: string
  title: string
  type: MockArtifactType
  /** 来源会话名（演示） */
  source: string
  /** 预览与导出使用的纯文本内容 */
  text: string
}

export const mockArtifacts: MockArtifact[] = [
  {
    id: 'a1',
    title: 'Agent 评测维度整理',
    type: '笔记',
    source: '调研 Agent 评测方法',
    text: '# Agent 评测维度整理\n\n这是一份模拟笔记，用于展示成果预览。\n\n- 任务是否完成\n- 工具调用是否正确\n- 成本和时延\n- 异常与恢复表现\n\n真实来源与引用在后端接入后补齐。',
  },
  {
    id: 'a2',
    title: '分页边界修复记录',
    type: '代码变更',
    source: 'Mini DeerFlow / 修复分页边界',
    text: '示例代码变更（模拟，未实际改动仓库）：\n\nsrc/pagination.py\n    if page < 1 or size < 1:\n        raise ValueError("invalid pagination")\n    start = (page - 1) * size\n\ntests/test_pagination.py\n    def test_first_page():\n        assert paginate([1, 2, 3], 1, 2) == [1, 2]\n',
  },
  {
    id: 'a3',
    title: '上下文工程学习提纲',
    type: '文档',
    source: '学习上下文工程',
    text: '# 上下文工程学习提纲\n\n模拟文档：\n1. 输入预算\n2. 消息组装\n3. 工具结果裁剪\n4. 历史压缩与摘要\n5. 记忆隔离与检索',
  },
]
