"""Create the competition-ready editable DOCX technical document."""
from pathlib import Path
import re
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.shared import Inches, Pt, RGBColor
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'docs' / 'VeraLane技术文档.md'
OUTPUT = ROOT / 'docs' / 'VeraLane技术文档.docx'
DIAGRAM = ROOT / 'docs' / 'VeraLane系统架构图.png'
FONT_PATH = Path(r'C:\Windows\Fonts\msyh.ttc')
BLACK = '000000'
INK = '202B38'
BLUE = '244A6A'
PALE = 'F2F5F8'
BORDER = 'D9D9D9'


def font(size, index=0):
    return ImageFont.truetype(str(FONT_PATH), size, index=index)


def draw_architecture():
    w, h = 2200, 930
    im = Image.new('RGB', (w, h), '#FFFFFF')
    d = ImageDraw.Draw(im)
    d.text((62, 24), 'VeraLane 系统架构（竞赛演示版）', font=font(48), fill='#000000')
    labels = [
        ('用户浏览器', '自然语言目标', '查询 / 确认'),
        ('Web 前端', 'React + TypeScript', '对话 / 场景面板'),
        ('API 服务', 'FastAPI 路由', 'Pydantic 输入校验'),
        ('意图解析', '规则优先', 'DeepSeek 可选；只输出受限字段'),
        ('业务服务', '固定工具与计划', '余额 / 账单 / 转账 / 卡 / 理财'),
        ('执行控制', '授权分级与快照', '挑战 / 重检 / 幂等'),
    ]
    x0, gap, bw, by, bh = 45, 22, 340, 132, 235
    fills = ['#EEF4FA', '#EEF4FA', '#EEF4FA', '#F6F7F9', '#EEF4FA', '#FFF5E6']
    for i, (head, line1, line2) in enumerate(labels):
        x = x0 + i * (bw + gap)
        d.rounded_rectangle((x, by, x + bw, by + bh), radius=24, fill=fills[i], outline='#9AAABD', width=3)
        d.text((x + 24, by + 24), head, font=font(34), fill='#142C43')
        d.text((x + 24, by + 92), line1, font=font(25), fill='#244A6A')
        # Wrap the final label line for the parser and tool boxes.
        if i == 3:
            d.text((x + 24, by + 139), '仅生成受限意图字段', font=font(23), fill='#43556A')
        elif i == 4:
            d.text((x + 24, by + 139), '支出工具均走服务端规则', font=font(23), fill='#43556A')
        elif i == 5:
            d.text((x + 24, by + 139), '确认后再次校验状态', font=font(23), fill='#43556A')
        else:
            d.text((x + 24, by + 145), line2, font=font(23), fill='#43556A')
        if i < len(labels) - 1:
            ax = x + bw + 5
            ay = by + 116
            nx = ax + gap - 10
            d.line((ax, ay, nx, ay), fill='#55728F', width=5)
            d.polygon([(nx, ay), (nx - 13, ay - 10), (nx - 13, ay + 10)], fill='#55728F')

    # Scheduler and persistence form the control/data layer below the main flow.
    boxes = [
        (170, 500, 760, 660, '业务时钟 / 调度器', '只推进已授权且仍在有效窗口内的事件'),
        (1010, 500, 1740, 660, 'SQLite 模拟账本与审计', 'BEGIN IMMEDIATE · 唯一键 · 原子回执 · 虚构数据'),
    ]
    for x1, y1, x2, y2, head, body in boxes:
        d.rounded_rectangle((x1, y1, x2, y2), radius=22, fill='#F7F8FA', outline='#9AAABD', width=3)
        d.text((x1 + 26, y1 + 28), head, font=font(31), fill='#142C43')
        d.text((x1 + 26, y1 + 91), body, font=font(22), fill='#43556A')
    # Scheduler feeds fixed tools; authorization layer writes an atomic ledger record.
    d.line((470, 500, 470, 426, 1663, 426, 1663, 370), fill='#55728F', width=4)
    d.polygon([(1663, 370), (1653, 386), (1673, 386)], fill='#55728F')
    d.text((615, 438), '到期事件经服务端重新核验', font=font(20), fill='#43556A')
    d.line((2025, 367, 2025, 458, 1375, 458, 1375, 500), fill='#55728F', width=4)
    d.polygon([(1375, 500), (1365, 484), (1385, 484)], fill='#55728F')
    d.text((1500, 438), '确认后的工具调用', font=font(20), fill='#43556A')

    # Deployment boundary is stated separately from the application identity boundary.
    d.rounded_rectangle((45, 735, 2155, 890), radius=20, fill='#F2F5F8', outline='#C4CCD5', width=2)
    d.text((75, 757), '云端演示入口', font=font(28), fill='#142C43')
    d.text((380, 757), '浏览器  →  HTTPS / Nginx 共享 Basic Auth  →  127.0.0.1:8000 应用', font=font(26), fill='#244A6A')
    d.text((380, 810), '共享口令只保护演示入口；应用本身没有用户登录、MFA 或多账户隔离。当前演示为离线模式。', font=font(22), fill='#43556A')
    im.save(DIAGRAM, dpi=(300, 300))


def set_font(run, size=11, bold=None, color=INK, mono=False):
    run.font.name = 'Consolas' if mono else 'Aptos'
    run._element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
    run.font.size = Pt(size)
    run.font.color.rgb = RGBColor.from_string(color)
    if bold is not None:
        run.bold = bold


def add_field(paragraph, instruction):
    run = paragraph.add_run()
    begin = OxmlElement('w:fldChar'); begin.set(qn('w:fldCharType'), 'begin')
    instr = OxmlElement('w:instrText'); instr.set(qn('xml:space'), 'preserve'); instr.text = instruction
    separate = OxmlElement('w:fldChar'); separate.set(qn('w:fldCharType'), 'separate')
    value = OxmlElement('w:t'); value.text = '1'
    end = OxmlElement('w:fldChar'); end.set(qn('w:fldCharType'), 'end')
    run._r.extend([begin, instr, separate, value, end])


def set_cell_shading(cell, fill):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn('w:shd'))
    if shd is None:
        shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:fill'), fill)
    if shd.getparent() is not None:
        shd.getparent().remove(shd)
    ordered = [qn(f'w:{name}') for name in ('tcW', 'gridSpan', 'hMerge', 'vMerge', 'tcBorders')]
    insert_at = max((i + 1 for i, child in enumerate(tc_pr) if child.tag in ordered), default=0)
    tc_pr.insert(insert_at, shd)


def set_cell_margins(cell, top=110, start=120, bottom=110, end=120):
    tc = cell._tc; tc_pr = tc.get_or_add_tcPr()
    margins = tc_pr.first_child_found_in('w:tcMar')
    if margins is None:
        margins = OxmlElement('w:tcMar')
    elif margins.getparent() is not None:
        margins.getparent().remove(margins)
    for side, value in [('top', top), ('start', start), ('bottom', bottom), ('end', end)]:
        node = margins.find(qn(f'w:{side}'))
        if node is None:
            node = OxmlElement(f'w:{side}'); margins.append(node)
        node.set(qn('w:w'), str(value)); node.set(qn('w:type'), 'dxa')
    later = [qn(f'w:{name}') for name in ('textDirection', 'tcFitText', 'vAlign', 'hideMark', 'headers', 'cellIns', 'cellDel', 'cellMerge', 'tcPrChange')]
    insert_at = next((i for i, child in enumerate(tc_pr) if child.tag in later), len(tc_pr))
    tc_pr.insert(insert_at, margins)


def add_rich(p, text, table=False):
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    token_re = re.compile(r'(\*\*.*?\*\*|`[^`]+`)')
    pos = 0
    for m in token_re.finditer(text):
        if m.start() > pos:
            r = p.add_run(text[pos:m.start()]); set_font(r, 9.3 if table else 11)
        token = m.group(0)
        if token.startswith('**'):
            r = p.add_run(token[2:-2]); set_font(r, 9.3 if table else 11, bold=True)
        else:
            r = p.add_run(token[1:-1]); set_font(r, 8.8 if table else 9.5, color='34495E', mono=True)
        pos = m.end()
    if pos < len(text):
        r = p.add_run(text[pos:]); set_font(r, 9.3 if table else 11)


def add_page_field(paragraph):
    add_field(paragraph, 'PAGE')


def setup_doc():
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Inches(8.5), Inches(11)
    sec.top_margin, sec.bottom_margin = Inches(.72), Inches(.7)
    sec.left_margin, sec.right_margin = Inches(.78), Inches(.78)
    normal = doc.styles['Normal']
    normal.font.name = 'Aptos'; normal._element.rPr.rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
    normal.font.size = Pt(11); normal.font.color.rgb = RGBColor.from_string(INK)
    normal.paragraph_format.space_after = Pt(7); normal.paragraph_format.line_spacing = 1.12
    for name, size in [('Heading 1', 17), ('Heading 2', 13.5), ('Heading 3', 11.5)]:
        style = doc.styles[name]
        style.font.name = 'Aptos Display'; style._element.rPr.rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
        style.font.size = Pt(size); style.font.bold = True; style.font.color.rgb = RGBColor.from_string(BLACK)
        style.paragraph_format.space_before = Pt(16 if name == 'Heading 1' else 11)
        style.paragraph_format.space_after = Pt(6); style.paragraph_format.keep_with_next = True
    zoom = doc.settings.element.find(qn('w:zoom'))
    if zoom is not None:
        zoom.set(qn('w:percent'), '100')
    header = sec.header.paragraphs[0]
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    r = header.add_run('VERALANE  |  技术文档 v1.1'); set_font(r, 8, color='68788A')
    footer = sec.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = footer.add_run('竞赛原型 · 虚构数据 · 受控演示  |  第 '); set_font(r, 8, color='68788A')
    add_page_field(footer)
    r = footer.add_run(' 页'); set_font(r, 8, color='68788A')
    return doc


def add_front_matter(doc, headings):
    title = doc.add_paragraph()
    title.paragraph_format.space_before = Pt(80); title.paragraph_format.space_after = Pt(18)
    r = title.add_run('VeraLane 技术文档'); set_font(r, 30, bold=True, color=BLACK)
    subtitle = doc.add_paragraph()
    subtitle.paragraph_format.space_after = Pt(28)
    r = subtitle.add_run('AI Banking Agent  |  系统架构 · 核心算法 · 安全设计'); set_font(r, 14, color='42566B')
    rows = [
        ('文档版本', '参赛原型交付稿 v1.1'),
        ('编制日期', '2026-10-09'),
        ('系统范围', '本机可复现原型与受控云端演示'),
        ('部署入口', 'https://demo.wuyeni.cn（HTTPS + Nginx 共享口令）'),
        ('数据与业务', '全为虚构演示数据；不连接真实银行、支付机构或商户'),
        ('模型模式', '默认 offline；云端演示未配置模型 API 密钥'),
        ('代码仓库', '私有 GitHub：yeniwu46-max/VeraLane'),
    ]
    table = doc.add_table(rows=0, cols=2); table.style = 'Table Grid'; table.alignment = WD_TABLE_ALIGNMENT.LEFT; table.autofit = False
    table.columns[0].width = Inches(1.35); table.columns[1].width = Inches(5.6)
    for key, value in rows:
        cells = table.add_row().cells
        cells[0].width = Inches(1.35); cells[1].width = Inches(5.6)
        for i, cell in enumerate(cells):
            set_cell_margins(cell); cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            p = cell.paragraphs[0]; p.paragraph_format.space_after = Pt(1)
            r = p.add_run(key if i == 0 else value); set_font(r, 10.5, bold=(i == 0), color='263B50' if i == 0 else INK)
            if i == 0: set_cell_shading(cell, 'EEF2F6')
    para = doc.add_paragraph()
    para.paragraph_format.space_before = Pt(22); para.paragraph_format.space_after = Pt(8)
    r = para.add_run('文档目的'); set_font(r, 13, bold=True, color=BLACK)
    para = doc.add_paragraph('说明 VeraLane 的系统边界、组件职责、主要 API、核心算法、授权与数据安全控制，并给出本机复现和云端演示的核验方式。本文描述的是参赛原型的实现证据与限制，不构成银行生产系统认证、合规审计或独立渗透测试结论。')
    for r in para.runs: set_font(r, 11)
    doc.add_page_break()
    h = doc.add_heading('目录', level=1)
    for heading in headings:
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Inches(.18); p.paragraph_format.space_after = Pt(5)
        r = p.add_run(heading); set_font(r, 11, color=INK)
    doc.add_page_break()


def markdown_table(lines, doc):
    rows = []
    for line in lines:
        cells = [x.strip() for x in line.strip().strip('|').split('|')]
        if all(re.fullmatch(r':?-{3,}:?', x.replace(' ', '')) for x in cells):
            continue
        rows.append(cells)
    if not rows:
        return
    ncols = max(len(row) for row in rows)
    table = doc.add_table(rows=len(rows), cols=ncols)
    table.style = 'Table Grid'; table.alignment = WD_TABLE_ALIGNMENT.CENTER; table.autofit = False
    widths_by_n = {
        2: [1.7, 5.2],
        3: [1.2, 3.4, 2.3],
        4: [.85, 2.05, 2.45, 1.55],
    }
    widths = widths_by_n.get(ncols, [6.95 / ncols] * ncols)
    for ci, width in enumerate(widths): table.columns[ci].width = Inches(width)
    for ri, row in enumerate(rows):
        for ci in range(ncols):
            cell = table.cell(ri, ci); cell.width = Inches(widths[ci])
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            set_cell_margins(cell, top=110, bottom=110, start=115, end=115)
            if ri == 0: set_cell_shading(cell, '244A6A')
            elif ri % 2 == 0: set_cell_shading(cell, 'F2F5F8')
            p = cell.paragraphs[0]; p.paragraph_format.space_after = Pt(1); p.paragraph_format.line_spacing = 1.05
            add_rich(p, row[ci] if ci < len(row) else '', table=True)
            if ri == 0:
                for run in p.runs:
                    run.font.color.rgb = RGBColor(255, 255, 255); run.bold = True
    repeat = OxmlElement('w:tblHeader'); repeat.set(qn('w:val'), 'true')
    table.rows[0]._tr.get_or_add_trPr().append(repeat)
    spacer = doc.add_paragraph(); spacer.paragraph_format.space_after = Pt(3)


def add_architecture(doc):
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(3); p.paragraph_format.space_after = Pt(4)
    p.add_run().add_picture(str(DIAGRAM), width=Inches(6.9))
    cap = doc.add_paragraph('图 1  VeraLane 组件、授权/持久化链路与云端演示入口。模型仅参与受限意图解析；账本状态由服务端固定业务工具更新。')
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER; cap.paragraph_format.space_after = Pt(10)
    for r in cap.runs: set_font(r, 9, color='57697A')


def add_markdown_body(doc, lines):
    i = 0; in_code = False; code = []; code_lang = ''
    while i < len(lines):
        line = lines[i]; value = line.strip()
        if value.startswith('```'):
            if not in_code:
                in_code = True; code = []; code_lang = value[3:].strip()
            else:
                if code_lang == 'mermaid':
                    add_architecture(doc)
                else:
                    p = doc.add_paragraph()
                    p.paragraph_format.left_indent = Inches(.12); p.paragraph_format.right_indent = Inches(.08)
                    p.paragraph_format.space_before = Pt(3); p.paragraph_format.space_after = Pt(7)
                    for ci, item in enumerate(code):
                        if ci: p.add_run().add_break()
                        r = p.add_run(item); set_font(r, 8.5, color='293A4C', mono=True)
                in_code = False
            i += 1; continue
        if in_code:
            code.append(line); i += 1; continue
        if not value or value == '---':
            i += 1; continue
        if value.startswith('|'):
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                table_lines.append(lines[i]); i += 1
            markdown_table(table_lines, doc); continue
        if value.startswith('#'):
            level = min(len(value) - len(value.lstrip('#')), 3)
            heading = value[level:].strip()
            # The source title is represented by the cover page; all chapter headings remain semantic Word headings.
            if level == 1 and heading == 'VeraLane 技术文档':
                i += 1; continue
            p = doc.add_heading('', level=level)
            add_rich(p, heading)
            i += 1; continue
        if value.startswith('- ') or value.startswith('* '):
            p = doc.add_paragraph(style='List Bullet'); add_rich(p, value[2:]); i += 1; continue
        if re.match(r'^\d+\.\s', value):
            p = doc.add_paragraph(style='List Number'); add_rich(p, re.sub(r'^\d+\.\s', '', value)); i += 1; continue
        p = doc.add_paragraph(); add_rich(p, value); i += 1


def main():
    draw_architecture()
    lines = SOURCE.read_text(encoding='utf-8').splitlines()
    # Strip the original title and metadata line; these are replaced with controlled cover metadata.
    body = lines[1:]
    while body and not body[0].strip(): body.pop(0)
    if body and body[0].startswith('版本：'): body = body[1:]
    while body and not body[0].strip(): body.pop(0)
    headings = [line.strip().lstrip('#').strip() for line in body if line.startswith('## ')]
    doc = setup_doc()
    add_front_matter(doc, headings)
    add_markdown_body(doc, body)
    doc.core_properties.title = 'VeraLane 技术文档'
    doc.core_properties.subject = '系统架构、核心算法、安全设计与部署说明'
    doc.core_properties.author = 'VeraLane'
    doc.core_properties.keywords = 'AI Banking, Agent, 安全设计, 系统架构'
    doc.save(OUTPUT)
    print(f'Wrote {OUTPUT}')
    print(f'Wrote {DIAGRAM}')


if __name__ == '__main__':
    main()
