const pptxgen = require('pptxgenjs');
const path = require('node:path');

const pptx = new pptxgen();
pptx.layout = 'LAYOUT_WIDE';
pptx.author = 'VeraLane';
pptx.subject = '2026 fintech hackathon · AI Banking';
pptx.title = 'VeraLane — Banking, verified.';
pptx.company = 'VeraLane';
pptx.lang = 'zh-CN';
pptx.theme = {
  headFontFace: 'Microsoft YaHei',
  bodyFontFace: 'Microsoft YaHei',
  lang: 'zh-CN',
};
pptx.defineLayout({ name: 'CUSTOM_WIDE', width: 13.333, height: 7.5 });
pptx.layout = 'CUSTOM_WIDE';
pptx.margin = 0;

const W = 13.333;
const H = 7.5;
const C = {
  bg: '0A1220',
  panel: '111F33',
  panel2: '172941',
  panel3: '0E1929',
  blue: '61AFFF',
  cyan: '8ADCF0',
  amber: 'F4B85A',
  red: 'EF7773',
  white: 'F2F6FC',
  body: 'C9D5E6',
  muted: '8EA1BA',
  line: '293D58',
};
const F = 'Microsoft YaHei';
const A = path.resolve(__dirname, '../docs/screenshots');

function text(slide, value, x, y, w, h, opts = {}) {
  slide.addText(value, {
    x, y, w, h,
    fontFace: F,
    fontSize: 16,
    color: C.body,
    margin: 0,
    breakLine: false,
    valign: 'mid',
    fit: 'shrink',
    ...opts,
  });
}

function rect(slide, x, y, w, h, fill, line = fill, radius = 0.16) {
  slide.addShape(pptx.ShapeType.roundRect, {
    x, y, w, h,
    rectRadius: radius,
    fill: { color: fill },
    line: { color: line, width: 0.8 },
  });
}

function addBase(slide, n, section, dark = true) {
  slide.background = { color: C.bg };
  text(slide, 'VERALANE   /   AI BANKING', 0.62, 0.33, 4.5, 0.22, {
    fontSize: 9, bold: true, charSpacing: 1.6, color: C.blue,
  });
  text(slide, section.toUpperCase(), 9.5, 0.33, 3.2, 0.22, {
    fontSize: 9, bold: true, charSpacing: 1.2, color: C.muted, align: 'right',
  });
  text(slide, String(n).padStart(2, '0'), 12.25, 7.05, 0.42, 0.2, {
    fontSize: 10, bold: true, color: C.muted, align: 'right',
  });
  if (!dark) slide.background = { color: C.panel3 };
}

function title(slide, heading, sub, y = 0.82) {
  text(slide, heading, 0.7, y, 11.9, 0.62, {
    fontSize: 29, bold: true, color: C.white, valign: 'mid',
  });
  if (sub) text(slide, sub, 0.72, y + 0.73, 11.65, 0.38, {
    fontSize: 13, color: C.muted, valign: 'top',
  });
}

function card(slide, x, y, w, h, eyebrow, heading, body, accent = C.blue) {
  rect(slide, x, y, w, h, C.panel, C.line);
  slide.addShape(pptx.ShapeType.rect, {
    x, y: y + 0.16, w: 0.045, h: h - 0.32,
    fill: { color: accent }, line: { color: accent, transparency: 100 },
  });
  if (eyebrow) text(slide, eyebrow, x + 0.24, y + 0.22, w - 0.45, 0.24, {
    fontSize: 10, bold: true, color: accent, charSpacing: 0.7,
  });
  text(slide, heading, x + 0.24, y + 0.63, w - 0.48, 0.48, {
    fontSize: 18, bold: true, color: C.white,
  });
  text(slide, body, x + 0.24, y + 1.22, w - 0.48, h - 1.42, {
    fontSize: 12, color: C.body, valign: 'top', breakLine: true,
    lineSpacingMultiple: 1.08,
  });
}

function screenshot(slide, filename, x, y, w, h) {
  rect(slide, x - 0.04, y - 0.04, w + 0.08, h + 0.08, C.panel, C.line, 0.12);
  slide.addImage({
    path: path.join(A, filename), x, y, w, h,
    altText: filename.replaceAll('-', ' ').replace('.jpg', ''),
  });
}

function notes(slide, script) {
  slide.addNotes(script);
}

// 1. Opening
{
  const s = pptx.addSlide(); addBase(s, 1, '可信执行');
  text(s, 'BANKING, VERIFIED.', 0.8, 1.03, 5.6, 0.32, {
    fontSize: 13, bold: true, charSpacing: 2.1, color: C.amber,
  });
  text(s, '让每一步\n都有据可查', 0.78, 1.7, 7.1, 1.9, {
    fontSize: 39, bold: true, color: C.white, valign: 'mid', breakLine: true,
    lineSpacingMultiple: 0.98,
  });
  text(s, 'VeraLane  /  AI Banking Agent', 0.84, 4.07, 5.2, 0.35, {
    fontSize: 18, color: C.blue, bold: true,
  });
  text(s, '自然语言进入  ·  工具核对  ·  用户授权  ·  可追溯回执', 0.84, 4.62, 6.2, 0.36, {
    fontSize: 13, color: C.body,
  });
  // A visual motif: one request passing through three verified gates.
  const nodes = [
    { x: 8.4, y: 2.1, label: 'UNDERSTAND', sub: '理解需求', c: C.blue },
    { x: 10.1, y: 3.4, label: 'VERIFY', sub: '核对数据', c: C.cyan },
    { x: 8.35, y: 4.75, label: 'AUTHORIZE', sub: '确认后执行', c: C.amber },
  ];
  s.addShape(pptx.ShapeType.line, { x: 8.8, y: 2.6, w: 1.48, h: 0.95, line: { color: C.line, width: 3 } });
  s.addShape(pptx.ShapeType.line, { x: 8.8, y: 3.78, w: 1.48, h: 0.94, line: { color: C.line, width: 3 } });
  nodes.forEach((p, i) => {
    s.addShape(pptx.ShapeType.ellipse, { x: p.x, y: p.y, w: 1.3, h: 1.3, fill: { color: C.panel2 }, line: { color: p.c, width: 1.6 } });
    text(s, String(i + 1).padStart(2, '0'), p.x, p.y + 0.23, 1.3, 0.34, { fontSize: 20, bold: true, color: p.c, align: 'center' });
    text(s, p.label, p.x - 0.58, p.y + 1.42, 2.45, 0.22, { fontSize: 9, bold: true, charSpacing: 1, color: p.c, align: 'center' });
    text(s, p.sub, p.x - 0.58, p.y + 1.69, 2.45, 0.23, { fontSize: 10, color: C.body, align: 'center' });
  });
  text(s, '深圳国际金融科技大赛  ·  人工智能赛道', 0.84, 6.65, 7.6, 0.25, { fontSize: 10, color: C.muted });
  notes(s, '各位评委好，我的项目叫 VeraLane，定位是一款“每一步都可核验”的 AI Banking 智能体。它让用户用自然语言提出银行业务需求，但不把资金操作交给模型自由发挥。我们把意图理解、银行工具校验、用户授权和操作回执连接起来，让每一步都有据可查。');
}

// 2. Problem framing
{
  const s = pptx.addSlide(); addBase(s, 2, '问题与定位');
  title(s, '理解需求只是起点', '银行操作还要回答：对象是谁、影响什么、是谁批准、结果如何核对？');
  card(s, 0.72, 2.1, 3.85, 2.72, '01 / AMBIGUITY', '口语信息不完整', '“转给王明三百元”可能匹配多个联系人。\n“少花三百”不代表可以自行取消协议。', C.blue);
  card(s, 4.75, 2.1, 3.85, 2.72, '02 / CONSEQUENCE', '操作影响真实', '对象或金额猜错会改变资金状态。\n计划必须先展示影响，再请求授权。', C.amber);
  card(s, 8.78, 2.1, 3.85, 2.72, '03 / EVIDENCE', '结果需要核验', '执行后提供逐项回执与本地证据。\n不把“模型说成功”当作交易完成。', C.cyan);
  const steps = [['自然语言', C.blue], ['核验计划', C.cyan], ['明确授权', C.amber], ['操作回执', C.blue]];
  steps.forEach((v, i) => {
    const x = 1.0 + i * 3.0;
    rect(s, x, 5.55, 2.25, 0.65, C.panel2, C.line, 0.1);
    text(s, v[0], x + 0.1, 5.74, 2.05, 0.24, { fontSize: 15, bold: true, color: v[1], align: 'center' });
    if (i < steps.length - 1) text(s, '›', x + 2.42, 5.69, 0.42, 0.35, { fontSize: 29, bold: true, color: C.muted, align: 'center' });
  });
  notes(s, '银行业务很适合自然语言交互，却不适合模糊执行。“转给王明三百元”可能有多个同名联系人；“帮我少花三百”也不代表系统可以擅自取消协议。关键不只是听懂句子，还要核对对象、金额、影响范围和授权状态。VeraLane 的核心原则是：先把需求变成清晰计划，再让用户确认，最后由确定性业务工具执行。');
}

// 3. Architecture
{
  const s = pptx.addSlide(); addBase(s, 3, '系统架构');
  title(s, '模型负责理解，工具负责执行', 'AI 输出被限制为意图字段；资金状态只由经过复核的服务端工具改变。');
  const items = [
    { x: 0.72, w: 2.18, n: '01', h: '对话输入', b: '自然语言\n交易/联系人证据', c: C.blue },
    { x: 3.12, w: 2.18, n: '02', h: '受限意图', b: '规则优先\n模型可选、离线可用', c: C.cyan },
    { x: 5.52, w: 2.18, n: '03', h: '确定性校验', b: '金额 / 对象 / 状态\n权限与快照复核', c: C.amber },
    { x: 7.92, w: 2.18, n: '04', h: '用户授权', b: '确认计划\n高风险追加挑战', c: C.red },
    { x: 10.32, w: 2.25, n: '05', h: '模拟账本', b: 'SQLite 事务\n审计与操作回执', c: C.blue },
  ];
  items.forEach((it, i) => {
    rect(s, it.x, 2.15, it.w, 2.18, C.panel, C.line);
    text(s, it.n, it.x + 0.2, 2.4, it.w - 0.4, 0.3, { fontSize: 13, bold: true, color: it.c });
    text(s, it.h, it.x + 0.2, 2.88, it.w - 0.4, 0.38, { fontSize: 17, bold: true, color: C.white });
    text(s, it.b, it.x + 0.2, 3.42, it.w - 0.4, 0.6, { fontSize: 11, color: C.body, valign: 'top', breakLine: true });
    if (i < items.length - 1) s.addShape(pptx.ShapeType.line, {
      x: it.x + it.w + 0.04, y: 3.21, w: 0.31, h: 0,
      line: { color: C.blue, width: 1.6, endArrowType: 'triangle' },
    });
  });
  rect(s, 2.15, 5.05, 9.0, 0.92, C.panel3, C.line);
  text(s, '安全边界', 2.45, 5.25, 1.28, 0.24, { fontSize: 12, bold: true, color: C.amber });
  text(s, '没有直接模型写账本的路径   ·   执行前复核金额与状态   ·   幂等回执防重复提交', 3.9, 5.23, 6.9, 0.3, { fontSize: 13, color: C.white, align: 'center' });
  text(s, '模型输出是待核对的输入，不是授权，也不是银行回执。', 2.25, 6.27, 8.8, 0.28, { fontSize: 13, color: C.muted, align: 'center' });
  notes(s, '架构从用户对话开始。离线规则或可选模型只负责提取受限的意图字段；服务端再对联系人、金额、账单证据和业务状态做校验，并生成待确认计划。权限层决定这是普通确认，还是还需要模拟强验证。用户确认后，固定工具在 SQLite 事务中更新虚构账本，并生成回执和审计记录。模型不能写数据库、调用任意代码或声称一个尚未发生的业务已经完成。');
}

// 4. Evidence-led bill analysis
{
  const s = pptx.addSlide(); addBase(s, 4, '账单分析');
  title(s, '从“花了多少”到可追溯证据', '回答附带期间、分类、笔数和来源流水；缺少可比基数时明确说明。');
  screenshot(s, 'insights-query.jpg', 0.72, 2.0, 7.1, 3.99);
  rect(s, 8.15, 2.0, 4.45, 3.99, C.panel, C.line);
  text(s, '2026 / 08   ·   餐饮', 8.55, 2.4, 3.7, 0.25, { fontSize: 12, bold: true, color: C.blue, charSpacing: 0.5 });
  text(s, '¥137', 8.52, 2.98, 3.4, 0.7, { fontSize: 38, bold: true, color: C.white });
  text(s, '1 笔支出   /   可打开原交易', 8.55, 3.76, 3.5, 0.3, { fontSize: 13, color: C.body });
  s.addShape(pptx.ShapeType.line, { x: 8.55, y: 4.33, w: 3.55, h: 0, line: { color: C.line, width: 1 } });
  text(s, '上期缺少可比基数', 8.55, 4.65, 3.5, 0.28, { fontSize: 15, bold: true, color: C.amber });
  text(s, '因此不计算增长率，\n并展示统计范围与口径。', 8.55, 5.08, 3.5, 0.6, { fontSize: 12, color: C.body, valign: 'top', breakLine: true });
  notes(s, '这里展示账单分析。用户询问八月餐饮支出，系统返回一笔、共一百三十七元，并可打开对应交易查看证据。由于样例上期没有可比基数，界面不会虚构增长百分比。查询结果会展示期间与口径，用户可以继续查看原始交易，而不只是看到一个没有来源的数字。');
}

// 5. Cross-scene spending plan
{
  const s = pptx.addSlide(); addBase(s, 5, '账单联动订阅');
  title(s, '目标可以跨场景拆成可确认的计划', '支出目标 → 找到可操作的订阅候选 → 用户选择 → 重算目标差额');
  screenshot(s, 'spending-plan-receipt.jpg', 6.4, 2.0, 6.2, 3.49);
  const rows = [
    ['目标', '下月少花 ¥300', C.blue],
    ['候选', '青柠音乐 ¥128', C.cyan],
    ['用户选择并确认', '仅取消这一项模拟代扣', C.amber],
    ['结果', '仍差 ¥172；不表示退款', C.red],
  ];
  rows.forEach((r, i) => {
    const y = 2.17 + i * 0.89;
    rect(s, 0.78, y, 5.16, 0.68, C.panel, C.line, 0.1);
    text(s, r[0], 1.02, y + 0.18, 1.35, 0.23, { fontSize: 11, bold: true, color: r[2] });
    text(s, r[1], 2.48, y + 0.17, 3.15, 0.26, { fontSize: 13, bold: true, color: C.white });
  });
  text(s, '协议取消不等于商户确认退订，也不产生退款。', 0.86, 5.95, 5.25, 0.28, { fontSize: 11, color: C.muted });
  notes(s, '接下来用户提出“下个月少花三百元”，系统把历史支出和订阅放进一个计划。用户选择青柠音乐后，系统展示预计减少一百二十八元，仍差一百七十二元。确认后只取消模拟代扣协议；这不代表商户已经退订，也不代表收到退款。可执行项和目标之间的差额会如实展示。');
}

// 6. Smart transfer
{
  const s = pptx.addSlide(); addBase(s, 6, '智能转账');
  title(s, '转账不是一句话就扣款', '让模糊对象停下来，让清晰计划可检查，让高风险操作多一道授权。');
  const stages = [
    { x: 0.78, n: 'A', label: '消歧', head: '“王明”有重名', body: '列出脱敏线索\n缺少选择就继续追问', c: C.blue },
    { x: 4.45, n: 'B', label: '计划', head: '收款人 / 金额 / 备注', body: '冻结本次操作快照\n展示执行前余额与限额', c: C.cyan },
    { x: 8.12, n: 'C', label: '授权', head: '确认后再次复核', body: '超阈值进入模拟挑战\n验证后仍需单独确认', c: C.amber },
  ];
  stages.forEach((it, i) => {
    rect(s, it.x, 2.2, 3.35, 2.62, C.panel, C.line);
    s.addShape(pptx.ShapeType.ellipse, { x: it.x + 0.25, y: 2.48, w: 0.55, h: 0.55, fill: { color: C.panel2 }, line: { color: it.c, width: 1.2 } });
    text(s, it.n, it.x + 0.25, 2.62, 0.55, 0.2, { fontSize: 13, bold: true, color: it.c, align: 'center' });
    text(s, it.label, it.x + 1.0, 2.62, 1.7, 0.22, { fontSize: 11, bold: true, color: it.c });
    text(s, it.head, it.x + 0.25, 3.36, 2.92, 0.35, { fontSize: 17, bold: true, color: C.white });
    text(s, it.body, it.x + 0.25, 3.94, 2.9, 0.55, { fontSize: 12, color: C.body, valign: 'top', breakLine: true });
    if (i < stages.length - 1) text(s, '→', it.x + 3.4, 3.31, 0.28, 0.42, { fontSize: 20, bold: true, color: C.muted, align: 'center' });
  });
  rect(s, 1.62, 5.42, 9.95, 0.76, C.panel3, C.line, 0.1);
  text(s, '相同操作 ID 重放既有回执；不会因为客户端重试而重复记账。', 1.9, 5.66, 9.36, 0.25, { fontSize: 13, bold: true, color: C.blue, align: 'center' });
  notes(s, '转账时，系统先解析收款人、金额和备注。遇到同名联系人会追问并展示脱敏线索；信息不足时不猜。明确联系人后，界面冻结收款对象和金额，先展示转账计划。用户确认时，后端会再次检查账户余额、日累计额度和联系人状态。超过规则阈值或属于高风险操作时，流程还需要模拟验证和另一项明确确认。相同操作重试会复用已有回执，避免因为网络重试重复扣款。');
}

// 7. Other domains
{
  const s = pptx.addSlide(); addBase(s, 7, '跨场景协同');
  title(s, '同一条授权链，覆盖不同业务', '每个场景都只推进到当前可证明的状态，不把计划误报成到账或完成。');
  screenshot(s, 'birthday-reservation.jpg', 0.74, 2.0, 6.05, 3.4);
  const blocks = [
    { y: 2.02, c: C.blue, h: '资金预留与生日任务', b: '预算先预留，再在任务窗口内模拟下单' },
    { y: 2.95, c: C.cyan, h: 'AA 收款与结算', b: '整数分分摊、逐笔回款与可追溯回执' },
    { y: 3.88, c: C.amber, h: '卡片与模拟支付', b: '卡状态由后端支付校验再次核对' },
    { y: 4.81, c: C.red, h: '投资申购与赎回', b: '展示评估、授权与待结算状态' },
  ];
  blocks.forEach(b => {
    rect(s, 7.18, b.y, 5.15, 0.72, C.panel, C.line, 0.1);
    text(s, b.h, 7.43, b.y + 0.12, 4.6, 0.24, { fontSize: 13, bold: true, color: b.c });
    text(s, b.b, 7.43, b.y + 0.4, 4.55, 0.2, { fontSize: 10, color: C.body });
  });
  text(s, '示例中 ¥1,000 仅为虚构生日任务预算；当前截图状态为预留中、未下单。', 0.8, 5.75, 11.55, 0.32, { fontSize: 12, bold: true, color: C.body });
  notes(s, '同一套计划和授权机制也用于其他金融场景：预约和周期转账有执行窗口；AA 收款按整数分核算并逐笔记录回款；卡片锁定后，模拟支付会在服务端再次检查卡状态；理财会展示风险评估和申赎状态；生日任务会先预留资金，再按阶段生成订单。这里展示的是虚构系统里的状态变化，不是银行、商户或支付机构的真实交易。');
}

// 8. Risk tiers
{
  const s = pptx.addSlide(); addBase(s, 8, '权限分级');
  title(s, '风险级别由后端决定', '模型和前端都不能自行降低某个操作的验证要求。');
  const columns = [
    { x: 0.8, color: C.blue, tag: 'READ', heading: '只读查询', lines: ['余额与账单', '逐笔交易证据', '只返回模拟数据'] },
    { x: 4.62, color: C.amber, tag: 'CONFIRM', heading: '黄级确认', lines: ['取消代扣 / 锁卡', '展示影响后确认', '执行时复核快照'] },
    { x: 8.44, color: C.red, tag: 'CHALLENGE + CONFIRM', heading: '红级挑战', lines: ['高金额或高风险动作', '当前计划绑定模拟挑战', '挑战后仍需单独确认'] },
  ];
  columns.forEach(col => {
    rect(s, col.x, 2.2, 3.42, 2.92, C.panel, C.line);
    rect(s, col.x + 0.22, 2.46, 1.2, 0.34, C.panel2, C.line, 0.08);
    text(s, col.tag, col.x + 0.3, 2.55, 1.06, 0.15, { fontSize: 8, bold: true, color: col.color, align: 'center', charSpacing: 0.4 });
    text(s, col.heading, col.x + 0.24, 3.04, 2.9, 0.36, { fontSize: 18, bold: true, color: C.white });
    col.lines.forEach((line, i) => {
      s.addShape(pptx.ShapeType.ellipse, { x: col.x + 0.26, y: 3.68 + i * 0.42, w: 0.09, h: 0.09, fill: { color: col.color }, line: { color: col.color } });
      text(s, line, col.x + 0.48, 3.62 + i * 0.42, 2.63, 0.22, { fontSize: 11, color: C.body });
    });
  });
  rect(s, 1.3, 5.63, 10.68, 0.66, C.panel3, C.line, 0.1);
  text(s, '转账严格大于 ¥1,000 触发红级；部分挂失、投资等高风险动作不依赖金额。', 1.58, 5.84, 10.1, 0.24, { fontSize: 12, color: C.amber, align: 'center', bold: true });
  text(s, '演示挑战码直接显示在页面，不是真实 OTP、短信验证或银行身份认证。', 2.0, 6.53, 9.2, 0.22, { fontSize: 10, color: C.muted, align: 'center' });
  notes(s, '查询只读；低风险变更要展示影响并确认；高风险操作要进行与当前计划绑定的模拟验证，再单独确认。执行时重新核对计划快照、金额、状态和权限，避免旧计划被修改后仍然执行。转账红级阈值严格大于一千元；挂失、投资申赎等部分高风险动作本身也进入红级。挑战码会在演示页面展示，不是真实 OTP 或银行身份认证。');
}

// 9. Evidence and limits
{
  const s = pptx.addSlide(); addBase(s, 9, '验证与边界');
  title(s, '把验证结果和能力边界一起报告', '不把规则回退算成模型成功，也不把原型安全描述成银行级安全。');
  rect(s, 0.82, 2.05, 5.48, 3.45, C.panel, C.line);
  text(s, 'LOCAL REGRESSION', 1.16, 2.4, 4.4, 0.25, { fontSize: 10, bold: true, charSpacing: 1.1, color: C.blue });
  text(s, '660', 1.1, 2.9, 2.4, 0.88, { fontSize: 52, bold: true, color: C.white });
  text(s, '后端测试通过', 1.2, 3.84, 3.8, 0.34, { fontSize: 18, bold: true, color: C.body });
  text(s, '前端 lint / build 通过\n另有浏览器场景与隔离数据库验收记录', 1.2, 4.48, 4.65, 0.65, { fontSize: 12, color: C.muted, valign: 'top', breakLine: true });
  rect(s, 6.72, 2.05, 5.74, 3.45, C.panel, C.line);
  text(s, 'MODEL EVALUATION · SYNTHETIC ONLY', 7.06, 2.4, 4.98, 0.25, { fontSize: 10, bold: true, charSpacing: 0.8, color: C.amber });
  const m = [['3', '合成请求'], ['2', 'HTTP 401'], ['1', '部分可解析']];
  m.forEach((v, i) => {
    const x = 7.05 + i * 1.64;
    text(s, v[0], x, 3.04, 1.2, 0.65, { fontSize: 35, bold: true, color: i === 2 ? C.cyan : C.white, align: 'center' });
    text(s, v[1], x - 0.12, 3.78, 1.45, 0.24, { fontSize: 10, color: C.body, align: 'center' });
  });
  text(s, '唯一成功响应未核验收款人和备注\n样本太少，不报告模型准确率', 7.12, 4.48, 4.8, 0.58, { fontSize: 12, color: C.body, align: 'center', valign: 'top', breakLine: true });
  notes(s, '仓库最近的回归记录为六百六十项后端测试通过，前端 lint 和生产构建通过，也有隔离数据库和浏览器场景记录。模型评测必须和规则降级分开：最近一次隔离在线评测发出三条合成请求，其中两条鉴权失败，一条解析出转账意图和三百元金额，但收款人及备注未核验。样本不足以估算准确率，所以目前演示默认离线，规则结果不冒充模型成绩。');
}

// 10. Deployment and close
{
  const s = pptx.addSlide(); addBase(s, 10, '交付与总结');
  title(s, '演示可访问，边界也可审查', '当前云端用于受控竞赛评审；项目不连接真实银行、不执行真实资金交易。');
  rect(s, 0.82, 2.1, 5.25, 2.88, C.panel, C.line);
  text(s, 'DEMO', 1.18, 2.46, 1.0, 0.22, { fontSize: 10, bold: true, charSpacing: 1, color: C.blue });
  text(s, 'demo.wuyeni.cn', 1.18, 2.95, 4.3, 0.45, { fontSize: 22, bold: true, color: C.white });
  text(s, 'HTTPS + 单一共享口令\n应用只监听服务器回环地址\n模型离线，数据为虚构演示账本', 1.18, 3.68, 4.4, 0.85, { fontSize: 13, color: C.body, valign: 'top', breakLine: true });
  rect(s, 6.54, 2.1, 5.9, 2.88, C.panel, C.line);
  text(s, 'WHAT REMAINS OUT OF SCOPE', 6.92, 2.46, 4.95, 0.22, { fontSize: 10, bold: true, charSpacing: 0.8, color: C.amber });
  text(s, '真实身份体系  /  多账户隔离  /  银行接口\n生产风控与清算  /  独立安全审查', 6.94, 3.08, 4.8, 0.88, { fontSize: 16, bold: true, color: C.white, valign: 'mid', breakLine: true });
  text(s, '让 AI 理解需求，让工具核对事实，让用户保留授权权。', 1.0, 5.7, 11.3, 0.45, { fontSize: 18, bold: true, color: C.blue, align: 'center' });
  text(s, 'BANKING, VERIFIED.   谢谢。', 3.25, 6.43, 6.8, 0.32, { fontSize: 16, bold: true, color: C.white, align: 'center', charSpacing: 0.7 });
  notes(s, '评委可通过受控的共享口令访问 HTTPS 演示站，源码包也提供部署步骤和复现说明。云端模型密钥为空，服务使用虚构数据；口令通过受控渠道另行提供。后续进入真实金融场景之前，仍需补充用户身份体系、按账户隔离、独立风控、真实验证和合规评估。VeraLane 当前证明的是一种可核验的 Agent 执行链路，而不是已经具备真实银行接入资质。让 AI 理解需求，让工具核对事实，让用户始终握有授权权。Banking, verified. 谢谢各位评委。');
}

pptx.writeFile({ fileName: path.resolve(__dirname, '../docs/VeraLane答辩PPT.pptx') })
  .then(() => console.log('Wrote docs/VeraLane答辩PPT.pptx'))
  .catch(error => { console.error(error); process.exitCode = 1; });
