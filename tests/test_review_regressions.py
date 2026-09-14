"""Regression cases found during the correctness and defensive review."""
import io
import json
import os
from types import SimpleNamespace

import pytest

from pdf_to_md import engine, mcp_server, ocr
from pdf_to_md.cli import main as cli_main
from pdf_to_md.equations import render_equation
from pdf_to_md.fileio import atomic_output
from pdf_to_md.geometry import Line
from pdf_to_md.sidebars import _line_boxes


def line(text, top, x0=10, x1=100, size=10, math=0):
    return Line(text, x0, x1, top, top + size, size, len(text), math=math)


@pytest.mark.parametrize('message', [None, [], 7, 'bad', {},
    {'jsonrpc': '1.0', 'id': 1, 'method': 'ping'},
    {'jsonrpc': '2.0', 'id': [], 'method': 'ping'},
    {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': []},
    {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': []}},
])
def test_bad_request_does_not_kill_server(message):
    output = io.StringIO()
    mcp_server.serve(io.StringIO(json.dumps(message) + '\n' + json.dumps(
        {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'}) + '\n'), output)
    replies = [json.loads(s) for s in output.getvalue().splitlines()]
    assert 'error' in replies[0]
    assert replies[-1]['id'] == 2 and replies[-1]['result'] == {}


def test_null_id_is_answered():
    assert mcp_server.handle_request({'jsonrpc': '2.0', 'id': None,
                                     'method': 'ping'})['result'] == {}


@pytest.mark.parametrize('limit', [0, -1, True, '10', None, 2.5])
def test_bad_limit_rejected_before_conversion(limit, monkeypatch, tmp_path):
    path = tmp_path / 'input.pdf'
    path.touch()
    monkeypatch.setattr(mcp_server, 'convert_pdf_to_markdown',
                        lambda *a, **k: pytest.fail('conversion must not run'))
    with pytest.raises(ValueError):
        mcp_server._tool_convert_pdf({'path': str(path), 'max_chars': limit})


@pytest.mark.parametrize('options', [{'lang': 'eng --tessdata-dir /tmp'},
    {'lang': '../eng'}, {'lang': ''}, {'lang': None}, {'psm': 0},
    {'psm': True}, {'dpi': 0}, {'dpi': 100000}, {'verify_numbers': 'false'}])
def test_bad_ocr_options_rejected(options):
    with pytest.raises(ValueError):
        ocr.OcrOptions(**options)


def test_valid_script_language():
    assert ocr.OcrOptions(lang='script/Latin+eng').lang == 'script/Latin+eng'


def test_cli_invalid_ocr_has_usage_error(capsys):
    with pytest.raises(SystemExit) as error:
        cli_main(['unused.pdf', '--ocr-dpi', '0'])
    assert error.value.code == 2
    assert 'Traceback' not in capsys.readouterr().err


@pytest.mark.parametrize('alias', ['same', 'symlink', 'hardlink'])
def test_source_cannot_be_overwritten(tmp_path, alias):
    source = tmp_path / 'source.pdf'
    source.write_bytes(b'original PDF')
    dest = source
    if alias != 'same':
        dest = tmp_path / 'output.md'
        if alias == 'symlink':
            dest.symlink_to(source)
        else:
            os.link(source, dest)
    with pytest.raises(ValueError, match='source PDF'):
        engine.convert_file(source, dest)
    assert source.read_bytes() == b'original PDF'


def test_failed_write_keeps_old_output_and_cleans_temporary(tmp_path):
    dest = tmp_path / 'out.md'
    dest.write_bytes(b'old')
    with pytest.raises(OSError):
        with atomic_output(dest) as output:
            output.write(b'partial')
            raise OSError('disk failure')
    assert dest.read_bytes() == b'old'
    assert list(tmp_path.iterdir()) == [dest]


def test_output_symlink_does_not_modify_target(tmp_path):
    target = tmp_path / 'target'
    target.write_bytes(b'keep')
    dest = tmp_path / 'out.md'
    dest.symlink_to(target)
    with atomic_output(dest) as output:
        output.write(b'new')
    assert target.read_bytes() == b'keep'
    assert dest.read_bytes() == b'new' and not dest.is_symlink()


def test_list_stops_at_heading_and_column_change():
    renderer = engine._Renderer(10, [16])
    lines = [line('- Item', 100), line('Heading', 112, size=16),
             line('- Next item', 140), line('Next column', 20, x0=200, x1=300)]
    blocks = renderer.render_lines(lines)
    assert blocks == ['- Item', '# Heading', '- Next item', 'Next column']


def test_figure_filename_is_encoded():
    fig = engine.FigureItem(0, 100, 0, 100, href='a b)#_assets/p1.png')
    assert engine._render_figure(fig) == ['![Figure](a%20b%29%23_assets/p1.png)']


def test_raw_equation_cannot_close_its_fence():
    result = render_equation(['x + ``` + y'], None)
    assert '\n````equation\n' in result and result.endswith('\n````')


def test_suspect_text_cannot_close_comment():
    result = engine._suspect_note(1, [{'text': '1', 'suspect': '--><img src=x>'}])
    assert result.count('-->') == 1 and '<img' not in result


def test_short_text_page_uses_its_text():
    assert not ocr.page_needs_ocr(SimpleNamespace(chars=[{'text': 'A'}], images=[]))
    assert ocr.page_needs_ocr(SimpleNamespace(chars=[{'text': ' '}]*10, images=[{}]))


def test_stacked_equation_is_not_emitted_twice():
    page = SimpleNamespace(width=600, height=800, lines=[], rects=[], chars=[])
    lines = [line('x + yz', 10, math=1), line('n', 20), line('a + bc', 21, math=1)]
    remaining, equations = engine._split_equations(page, lines, reconstruct_math=False)
    flattened = [text for equation in equations for text in equation.verbatim]
    assert flattened.count('a + bc') == 1


def test_separate_equations_do_not_merge_across_page():
    page = SimpleNamespace(width=600, height=800, lines=[], rects=[], chars=[])
    lines = [line('x + yz', 10, math=1), line('a + bc', 400, math=1)]
    assert len(engine._split_equations(page, lines, reconstruct_math=False)[1]) == 2


def test_box_detection_independent_of_drawing_order():
    edges = [dict(x0=10, x1=200, top=100, bottom=100),
             dict(x0=10, x1=200, top=10, bottom=10),
             dict(x0=10, x1=10, top=10, bottom=100),
             dict(x0=200, x1=200, top=10, bottom=100)]
    assert _line_boxes(SimpleNamespace(lines=edges)) == [(10, 10, 200, 100)]


def test_batch_basename_collision_is_reported(tmp_path, monkeypatch, capsys):
    import pdf_to_md.cli as cli
    first = tmp_path / 'a' / 'same.pdf'
    second = tmp_path / 'b' / 'same.pdf'
    for path in (first, second):
        path.parent.mkdir()
        path.touch()
    calls = []
    monkeypatch.setattr(cli, 'convert_file', lambda *a, **k: calls.append(a))
    assert cli_main([str(first), str(second), '-o', str(tmp_path / 'out'), '--json']) == 1
    result = json.loads(capsys.readouterr().out)
    assert len(calls) == 1 and result['failed'] == 1
    assert 'same output' in result['results'][1]['error']


@pytest.mark.parametrize('original,reread', [('1.2', '12'), ('-7', '7'), ('98%', '98')])
def test_value_changing_numeric_disagreement_is_flagged(original, reread, monkeypatch):
    monkeypatch.setattr(ocr, '_reread_digits', lambda *a: reread)
    words = [{'text': original, 'conf': 99}]
    ocr._flag_numbers(None, words, 1, ocr.OcrOptions())
    assert 'reread' in words[0]['suspect']


def test_ocr_retains_doubtful_number_and_bounds_subprocess(monkeypatch):
    import pytesseract
    from PIL import Image
    image = Image.new('L', (100, 100), 255)
    page = SimpleNamespace(width=24, height=24,
                           to_image=lambda **k: SimpleNamespace(original=image, bbox=(50, 60, 74, 84)))
    monkeypatch.setattr(ocr, 'find_tesseract', lambda: 'unused')
    monkeypatch.setattr(ocr, '_deskew', lambda img: img)
    calls = []
    def read(*args, **kwargs):
        calls.append(kwargs)
        return {'text': ['123', 'noise'], 'conf': ['12', '12'],
                'left': [5, 30], 'top': [5, 30], 'width': [10, 10], 'height': [10, 10]}
    monkeypatch.setattr(pytesseract, 'image_to_data', read)
    words, _, _ = ocr.ocr_page(page, ocr.OcrOptions(verify_numbers=False))
    assert len(words) == 1 and words[0]['text'] == '123'
    assert words[0]['suspect'] == 'confidence 12'
    assert words[0]['x0'] == pytest.approx(51.2)
    assert words[0]['top'] == pytest.approx(61.2)
    assert calls[0]['timeout'] == 120 and calls[0]['lang'] == 'eng'


def test_large_page_is_rejected_before_rendering(monkeypatch):
    monkeypatch.setattr(ocr, 'find_tesseract', lambda: 'unused')
    page = SimpleNamespace(width=100000, height=100000,
        to_image=lambda **k: pytest.fail('must reject before allocating'))
    with pytest.raises(ValueError, match='pixel limit'):
        ocr.ocr_page(page)


def test_symbol_negation_and_omicron_render_correctly():
    from pdf_to_md.mathsymbols import to_latex, to_unicode
    assert to_unicode('(cid:216)', 'Symbol') == '¬'
    assert to_latex('ο') == 'o'


def test_oversized_request_is_drained_and_next_request_succeeds(monkeypatch):
    monkeypatch.setattr(mcp_server, 'MAX_REQUEST_CHARS', 100)
    output = io.StringIO()
    mcp_server.serve(io.StringIO('x' * 350 + '\n' + json.dumps(
        {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'}) + '\n'), output)
    replies = [json.loads(s) for s in output.getvalue().splitlines()]
    assert len(replies) == 2 and replies[0]['error']['message'] == 'request too large'
    assert replies[1]['result'] == {}


@pytest.mark.parametrize('start,stop', [(0, float('inf')), (float('nan'), 3),
                                      (0, 1e9), (1e30, 1e30)])
def test_coordinate_scan_rejects_unbounded_geometry(start, stop):
    from pdf_to_md.geometry import scan_positions
    with pytest.raises(ValueError):
        list(scan_positions(start, stop, 1.5))


def test_batch_cannot_overwrite_another_input(tmp_path):
    from pdf_to_md.fileio import reserve_output
    source = tmp_path / 'another.md'
    source.write_bytes(b'PDF with unusual extension')
    with pytest.raises(ValueError, match='source PDF'):
        reserve_output(source, set(), [source])


def test_lattice_table_refuses_to_drop_unassigned_words():
    from pdf_to_md.tables import lattice_table
    words = [{'text': str(y), 'x0': 1, 'x1': 4, 'top': y, 'bottom': y + 2}
             for y in (2, 12, 22)]
    assert lattice_table(words, [0, 10], [0, 10, 20]) is None


def test_nested_embedded_fraction_is_reconstructed():
    from pdf_to_md.mathparse import reconstruct, Bar
    def ch(text, x, baseline=100):
        return dict(text=text, x0=x, x1=x+6, top=baseline-12,
                    bottom=baseline, size=12)
    chars = [ch('m', -50), ch('=', -40), ch('a', 30, 65), ch('b', 30, 85), ch('c', 30, 115)]
    assert reconstruct(chars, [Bar(20, 50, 95), Bar(26, 42, 70)]) == r'm = \frac{\frac{a}{b}}{c}'


def test_unexplained_coincident_bar_is_not_silently_discarded():
    from pdf_to_md.mathparse import reconstruct, Bar
    chars = [dict(text='a', x0=10, x1=16, top=78, bottom=90, size=12),
             dict(text='b', x0=10, x1=16, top=98, bottom=110, size=12)]
    assert reconstruct(chars, [Bar(8, 20, 95), Bar(9, 19, 95)]) is None


def test_empty_lattice_detection_preserves_body_words():
    table = SimpleNamespace(bbox=(0, 0, 100, 100), extract=lambda: [[None, None]])
    page = SimpleNamespace(width=600, height=800, lines=[], rects=[], images=[], curves=[],
        chars=[], find_tables=lambda: [table], extract_words=lambda **k: [
            dict(text='Preserve this body text', x0=10, x1=90, top=10, bottom=20,
                 size=10, fontname='Times-Roman')])
    stream = engine._parse_page_vector(page)
    assert any(item.kind == 'line' and item.text == 'Preserve this body text'
               for item in stream.items)


def test_table_cell_escapes_existing_backslash_before_pipe():
    assert engine._escape_cell('a\\|b\nc') == 'a\\\\\\|b c'


def test_ocr_orientation_mode_is_accepted():
    assert ocr.OcrOptions(psm=1).psm == 1


def test_figure_clip_uses_actual_page_origin(tmp_path):
    from PIL import Image
    from pdf_to_md.figures import extract_image
    captured = []
    def crop(box):
        captured.append(box)
        return SimpleNamespace(to_image=lambda **k: SimpleNamespace(
            original=Image.new('RGB', (10, 10))))
    page = SimpleNamespace(width=100, height=100, bbox=(50, 60, 150, 160), crop=crop)
    extract_image(page, (120, 130, 155, 165), tmp_path / 'figure.png')
    assert captured == [(120, 130, 150, 160)]
    assert (tmp_path / 'figure.png').is_file()
