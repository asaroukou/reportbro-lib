#
# Copyright (C) 2017 jobsta
#
# This file is part of ReportBro, is a library to generate PDF and Excel reports.
# Demos can be found at https://www.reportbro.com.
#
# ReportBro is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ReportBro is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

from __future__ import unicode_literals
from __future__ import division
import fpdf
import re
import xlsxwriter
import pkg_resources

from .elements import *
from .structs import Parameter, TextStyle
from .utils import get_int_value


try:
    basestring  # For Python 2, str and unicode
except NameError:
    basestring = str

regex_valid_identifier = re.compile(r'^[^\d\W]\w*$', re.U)


class Container:
    def __init__(self, container_id, containers, report):
        self.id = container_id
        self.report = report
        self.doc_elements = []
        self.width = 0
        self.height = 0
        containers[self.id] = self

    def add(self, doc_element):
        self.doc_elements.append(doc_element)

    def is_visible(self):
        return True


class ReportBand(Container):
    def __init__(self, band, container_id, containers, report):
        Container.__init__(self, container_id, containers, report)
        self.allow_page_break = False
        self.container_offset_y = 0
        self.sorted_elements = None
        self.render_elements = None
        self.explicit_page_break = True
        self.page_y = 0
        self.band = band
        self.width = report.document_properties.page_width -\
                report.document_properties.margin_left - report.document_properties.margin_right
        if band == BandType.content:
            self.allow_page_break = True
            self.height = report.document_properties.content_height
        elif band == BandType.header:
            self.height = report.document_properties.header_size
        elif band == BandType.footer:
            self.height = report.document_properties.footer_size

    def is_visible(self):
        if self.band == BandType.header:
            return self.report.document_properties.header
        elif self.band == BandType.footer:
            return self.report.document_properties.footer
        return True

    def prepare(self, ctx, pdf_doc=None, only_verify=False):
        self.sorted_elements = []
        for elem in self.doc_elements:
            if pdf_doc or not elem.spreadsheet_hide or only_verify:
                elem.prepare(ctx, pdf_doc=pdf_doc, only_verify=only_verify)
                if not self.allow_page_break:
                    # make sure element can be rendered multiple times (for header/footer)
                    elem.first_render_element = True
                    elem.rendering_complete = False
                self.sorted_elements.append(elem)

        if pdf_doc:
            self.sorted_elements = sorted(self.sorted_elements, key=lambda item: (item.y, item.sort_order))
            # predecessors are only needed for rendering pdf document
            for i, elem in enumerate(self.sorted_elements):
                predecessor = None
                for j in range(i-1, -1, -1):
                    elem2 = self.sorted_elements[j]
                    if elem2.bottom <= elem.y and\
                            (predecessor is None or elem2.bottom > predecessor.bottom):
                        predecessor = elem2
                if predecessor and not isinstance(predecessor, PageBreakElement):
                    elem.set_predecessor(predecessor)
            self.render_elements = []
        else:
            self.sorted_elements = sorted(self.sorted_elements, key=lambda item: (item.y, item.x))


    def create_render_elements(self, container_height, ctx, pdf_doc):
        i = 0
        new_page = False
        processed_elements = []
        completed_elements = dict()

        set_explicit_page_break = False
        while not new_page and i < len(self.sorted_elements):
            elem = self.sorted_elements[i]
            if elem.predecessor and (elem.predecessor.id not in completed_elements or
                    not elem.predecessor.rendering_complete):
                # predecessor is not completed yet -> start new page
                new_page = True
            else:
                elem_deleted = False
                if isinstance(elem, PageBreakElement):
                    if self.allow_page_break:
                        del self.sorted_elements[i]
                        elem_deleted = True
                        new_page = True
                        set_explicit_page_break = True
                        self.page_y = elem.y
                    else:
                        self.sorted_elements = []
                        return True
                else:
                    complete = False
                    if elem.predecessor:
                        # element is on same page as predecessor element so offset is relative to predecessor
                        offset_y = elem.predecessor.render_bottom + (elem.y - elem.predecessor.bottom)
                    else:
                        if self.allow_page_break:
                            if elem.first_render_element and self.explicit_page_break:
                                offset_y = elem.y - self.page_y
                            else:
                                offset_y = 0
                        else:
                            offset_y = elem.y

                    if elem.is_printed(ctx):
                        if offset_y >= container_height:
                            new_page = True
                        if not new_page:
                            render_elem, complete = elem.get_next_render_element(
                                offset_y, container_height=container_height, ctx=ctx, pdf_doc=pdf_doc)
                            if render_elem:
                                if complete:
                                    processed_elements.append(elem)
                                self.render_elements.append(render_elem)
                    else:
                        processed_elements.append(elem)
                        elem.finish_empty_element(offset_y)
                        complete = True
                    if complete:
                        completed_elements[elem.id] = True
                        del self.sorted_elements[i]
                        elem_deleted = True
                if not elem_deleted:
                    i += 1

        # in case of manual page break the element on the next page is positioned relative
        # to page break position
        self.explicit_page_break = set_explicit_page_break if self.allow_page_break else True

        if len(self.sorted_elements) > 0:
            self.render_elements.append(PageBreakElement(self.report, dict(y=-1)))
            for processed_element in processed_elements:
                # remove dependency to predecessor because successor element is either already added
                # to render_elements or on new page
                for successor in processed_element.successors:
                    successor.predecessor = None
        return len(self.sorted_elements) == 0

    def render_pdf(self, container_offset_x, container_offset_y, pdf_doc, cleanup=False):
        counter = 0
        for render_elem in self.render_elements:
            counter += 1
            if isinstance(render_elem, PageBreakElement):
                break
            render_elem.render_pdf(container_offset_x, container_offset_y, pdf_doc)
            if cleanup:
                render_elem.cleanup()
        self.render_elements = self.render_elements[counter:]

    def render_spreadsheet(self, row, ctx, workbook, worksheet):
        i = 0
        count = len(self.sorted_elements)
        while i < count:
            elem = self.sorted_elements[i]
            j = i + 1
            row_elements = [elem]
            while j < count:
                elem2 = self.sorted_elements[j]
                if elem2.y == elem.y:
                    row_elements.append(elem2)
                else:
                    break
                j += 1
            i = j
            col = 0
            current_row = row
            for row_element in row_elements:
                tmp_row = row_element.render_spreadsheet(current_row, col, ctx, workbook, worksheet)
                row = max(row, tmp_row)
                col += row_element.get_column_count() if isinstance(row_element, TableElement) else 1
        return row

    def is_finished(self):
        return len(self.render_elements) == 0

    def cleanup(self):
        for elem in self.doc_elements:
            elem.cleanup()


class DocumentPDFRenderer:
    def __init__(self, header_band, content_band, footer_band, report, context,
            additional_fonts, filename, add_watermark):
        self.header_band = header_band
        self.content_band = content_band
        self.footer_band = footer_band
        self.document_properties = report.document_properties
        self.pdf_doc = FPDFRB(report.document_properties, additional_fonts=additional_fonts)
        self.pdf_doc.set_margins(0, 0)
        self.pdf_doc.c_margin = 0  # interior cell margin
        self.context = context
        self.filename = filename
        self.add_watermark = add_watermark

    def add_page(self):
        self.pdf_doc.add_page()
        self.context.inc_page_number()

    def is_finished(self):
        return self.content_band.is_finished()

    def render(self):
        watermark_width = watermark_height = 0
        watermark_filename = pkg_resources.resource_filename('reportbro', 'data/logo_watermark.png')
        if self.add_watermark:
            if not os.path.exists(watermark_filename):
                self.add_watermark = False
            else:
                watermark_width = self.document_properties.page_width / 3
                watermark_height = watermark_width * (108 / 461)

        self.content_band.prepare(self.context, self.pdf_doc)
        page_count = 1
        while True:
            height = self.document_properties.page_height -\
                self.document_properties.margin_top - self.document_properties.margin_bottom
            if self.document_properties.header_display == BandDisplay.always or\
                    (self.document_properties.header_display == BandDisplay.not_on_first_page and page_count != 1):
                height -= self.document_properties.header_size
            if self.document_properties.footer_display == BandDisplay.always or\
                    (self.document_properties.footer_display == BandDisplay.not_on_first_page and page_count != 1):
                height -= self.document_properties.footer_size
            complete = self.content_band.create_render_elements(height, self.context, self.pdf_doc)
            if complete:
                break
            page_count += 1
            if page_count >= 10000:
                raise RuntimeError('Too many pages (probably an endless loop)')
        self.context.set_page_count(page_count)

        footer_offset_y = self.document_properties.page_height -\
            self.document_properties.footer_size - self.document_properties.margin_bottom
        # render at least one page to show header/footer even if content is empty
        while not self.content_band.is_finished() or self.context.get_page_number() == 0:
            self.add_page()
            if self.add_watermark:
                if watermark_height < self.document_properties.page_height:
                    self.pdf_doc.image(watermark_filename,
                            self.document_properties.page_width / 2 - watermark_width / 2,
                            self.document_properties.page_height - watermark_height,
                            watermark_width, watermark_height)

            content_offset_y = self.document_properties.margin_top
            page_number = self.context.get_page_number()
            if self.document_properties.header_display == BandDisplay.always or\
                    (self.document_properties.header_display == BandDisplay.not_on_first_page and page_number != 1):
                content_offset_y += self.document_properties.header_size
                self.header_band.prepare(self.context, self.pdf_doc)
                self.header_band.create_render_elements(self.document_properties.header_size,
                        self.context, self.pdf_doc)
                self.header_band.render_pdf(self.document_properties.margin_left,
                    self.document_properties.margin_top, self.pdf_doc)
            if self.document_properties.footer_display == BandDisplay.always or\
                    (self.document_properties.footer_display == BandDisplay.not_on_first_page and page_number != 1):
                self.footer_band.prepare(self.context, self.pdf_doc)
                self.footer_band.create_render_elements(self.document_properties.footer_size,
                        self.context, self.pdf_doc)
                self.footer_band.render_pdf(self.document_properties.margin_left, footer_offset_y, self.pdf_doc)

            self.content_band.render_pdf(self.document_properties.margin_left, content_offset_y, self.pdf_doc, cleanup=True)

        self.header_band.cleanup()
        self.footer_band.cleanup()
        dest = 'F' if self.filename else 'S'
        return self.pdf_doc.output(name=self.filename, dest=dest)


class DocumentXLSXRenderer:
    def __init__(self, header_band, content_band, footer_band, report, context, filename):
        self.header_band = header_band
        self.content_band = content_band
        self.footer_band = footer_band
        self.document_properties = report.document_properties
        self.workbook_mem = BytesIO()
        self.workbook = xlsxwriter.Workbook(filename if filename else self.workbook_mem)
        self.worksheet = self.workbook.add_worksheet()
        self.context = context
        self.filename = filename
        self.row = 0

    def render(self):
        if self.document_properties.header_display != BandDisplay.never:
            self.render_band(self.header_band)
        self.render_band(self.content_band)
        if self.document_properties.header_display != BandDisplay.never:
            self.render_band(self.footer_band)
        self.workbook.close()
        if not self.filename:
            # if no filename is given the spreadsheet data will be returned
            self.workbook_mem.seek(0)
            return self.workbook_mem.read()
        return None

    def render_band(self, band):
        band.prepare(self.context)
        self.row = band.render_spreadsheet(self.row, self.context, self.workbook, self.worksheet)


class DocumentProperties:
    def __init__(self, report, data):
        self.id = '0_document_properties'
        self.page_format = PageFormat[data.get('pageFormat').lower()]
        self.orientation = Orientation[data.get('orientation')]
        self.report = report

        if self.page_format == PageFormat.a4:
            if self.orientation == Orientation.portrait:
                self.page_width = 210
                self.page_height = 297
            else:
                self.page_width = 297
                self.page_height = 210
            unit = Unit.mm
        elif self.page_format == PageFormat.a5:
            if self.orientation == Orientation.portrait:
                self.page_width = 148
                self.page_height = 210
            else:
                self.page_width = 210
                self.page_height = 148
            unit = Unit.mm
        elif self.page_format == PageFormat.letter:
            if self.orientation == Orientation.portrait:
                self.page_width = 8.5
                self.page_height = 11
            else:
                self.page_width = 11
                self.page_height = 8.5
            unit = Unit.inch
        else:
            self.page_width = get_int_value(data, 'pageWidth')
            self.page_height = get_int_value(data, 'pageHeight')
            unit = Unit[data.get('unit')]
            if unit == Unit.mm:
                if self.page_width < 100 or self.page_width >= 100000:
                    self.report.errors.append(Error('errorMsgInvalidPageSize', object_id=self.id, field='page'))
                elif self.page_height < 100 or self.page_height >= 100000:
                    self.report.errors.append(Error('errorMsgInvalidPageSize', object_id=self.id, field='page'))
            elif unit == Unit.inch:
                if self.page_width < 1 or self.page_width >= 1000:
                    self.report.errors.append(Error('errorMsgInvalidPageSize', object_id=self.id, field='page'))
                elif self.page_height < 1 or self.page_height >= 1000:
                    self.report.errors.append(Error('errorMsgInvalidPageSize', object_id=self.id, field='page'))
        dpi = 72
        if unit == Unit.mm:
            self.page_width = round((dpi * self.page_width) / 25.4)
            self.page_height = round((dpi * self.page_height) / 25.4)
        else:
            self.page_width = round(dpi * self.page_width)
            self.page_height = round(dpi * self.page_height)

        self.content_height = get_int_value(data, 'contentHeight')
        self.margin_left = get_int_value(data, 'marginLeft')
        self.margin_top = get_int_value(data, 'marginTop')
        self.margin_right = get_int_value(data, 'marginRight')
        self.margin_bottom = get_int_value(data, 'marginBottom')
        self.pattern_locale = data.get('patternLocale', '')
        self.pattern_currency_symbol = data.get('patternCurrencySymbol', '')
        if self.pattern_locale not in ('de', 'en', 'es', 'fr', 'it'):
            raise RuntimeError('invalid pattern_locale')

        self.header = bool(data.get('header'))
        if self.header:
            self.header_display = BandDisplay[data.get('headerDisplay')]
            self.header_size = get_int_value(data, 'headerSize')
        else:
            self.header_display = BandDisplay.never
            self.header_size = 0
        self.footer = bool(data.get('footer'))
        if self.footer:
            self.footer_display = BandDisplay[data.get('footerDisplay')]
            self.footer_size = get_int_value(data, 'footerSize')
        else:
            self.footer_display = BandDisplay.never
            self.footer_size = 0
        if self.content_height == 0:
            self.content_height = self.page_height - self.header_size - self.footer_size -\
                self.margin_top - self.margin_bottom


class FPDFRB(fpdf.FPDF):
    def __init__(self, document_properties, additional_fonts):
        if document_properties.orientation == Orientation.portrait:
            orientation = 'P'
            dimension = (document_properties.page_width, document_properties.page_height)
        else:
            orientation = 'L'
            dimension = (document_properties.page_height, document_properties.page_width)
        fpdf.FPDF.__init__(self, orientation=orientation, unit='pt', format=dimension)
        self.x = 0
        self.y = 0
        self.set_doc_option('core_fonts_encoding', 'windows-1252')
        self.loaded_images = dict()
        self.available_fonts = dict(
            courier=dict(standard_font=True),
            helvetica=dict(standard_font=True),
            times=dict(standard_font=True))
        if additional_fonts:
            for additional_font in additional_fonts:
                filename = additional_font.get('filename', '')
                style_map = {'': '', 'B': 'B', 'I': 'I', 'BI': 'BI'}
                font = dict(standard_font=False, added=False, regular_filename=filename,
                        bold_filename=additional_font.get('bold_filename', filename),
                        italic_filename=additional_font.get('italic_filename', filename),
                        bold_italic_filename=additional_font.get('bold_italic_filename', filename),
                        style_map=style_map, uni=additional_font.get('uni', True))
                # map styles in case there are no separate font-files for bold, italic or bold italic
                # to avoid adding the same font multiple times to the pdf document
                if font['bold_filename'] == font['regular_filename']:
                    style_map['B'] = ''
                if font['italic_filename'] == font['bold_filename']:
                    style_map['I'] = style_map['B']
                elif font['italic_filename'] == font['regular_filename']:
                    style_map['I'] = ''
                if font['bold_italic_filename'] == font['italic_filename']:
                    style_map['BI'] = style_map['I']
                elif font['bold_italic_filename'] == font['bold_filename']:
                    style_map['BI'] = style_map['B']
                elif font['bold_italic_filename'] == font['regular_filename']:
                    style_map['BI'] = ''
                font['style2filename'] = {'': filename, 'B': font['bold_filename'],
                        'I': font['italic_filename'], 'BI': font['bold_italic_filename']}
                self.available_fonts[additional_font.get('value', '')] = font

    def add_image(self, img, image_key):
        self.loaded_images[image_key] = img

    def get_image(self, image_key):
        return self.loaded_images.get(image_key)

    def set_font(self, family, style='', size=0, underline=False):
        font = self.available_fonts.get(family)
        if font:
            if not font['standard_font']:
                if style:
                    # replace of 'U' is needed because it is set for underlined text
                    # when called from FPDF.add_page
                    style = font['style_map'].get(style.replace('U', ''))
                if not font['added']:
                    filename = font['style2filename'].get(style)
                    self.add_font(family, style=style, fname=filename, uni=font['uni'])
                    font['added'] = True
            if underline:
                style += 'U'
            fpdf.FPDF.set_font(self, family, style, size)

        
class Report:
    def __init__(self, report_definition, data, is_test_data=False, additional_fonts=None):
        assert isinstance(report_definition, dict)
        assert isinstance(data, dict)

        self.errors = []

        self.document_properties = DocumentProperties(self, report_definition.get('documentProperties'))

        self.containers = dict()
        self.header = ReportBand(BandType.header, '0_header', self.containers, self)
        self.content = ReportBand(BandType.content, '0_content', self.containers, self)
        self.footer = ReportBand(BandType.footer, '0_footer', self.containers, self)

        self.parameters = dict()
        self.styles = dict()
        self.data = data
        self.is_test_data = is_test_data

        self.additional_fonts = additional_fonts

        # list is needed to compute parameters (parameters with expression) in given order
        parameter_list = []
        for item in report_definition.get('parameters'):
            parameter = Parameter(self, item)
            if parameter.name in self.parameters:
                self.errors.append(Error('errorMsgDuplicateParameter', object_id=parameter.id, field='name'))
            self.parameters[parameter.name] = parameter
            parameter_list.append(parameter)

        for item in report_definition.get('styles'):
            style = TextStyle(item)
            style_id = int(item.get('id'))
            self.styles[style_id] = style

        for doc_element in report_definition.get('docElements'):
            element_type = DocElementType[doc_element.get('elementType')]
            container_id = doc_element.get('containerId')
            container = None
            if container_id:
                container = self.containers.get(container_id)
            elem = None
            if element_type == DocElementType.text:
                elem = TextElement(self, doc_element)
            elif element_type == DocElementType.line:
                elem = LineElement(self, doc_element)
            elif element_type == DocElementType.image:
                elem = ImageElement(self, doc_element)
            elif element_type == DocElementType.bar_code:
                elem = BarCodeElement(self, doc_element)
            elif element_type == DocElementType.table:
                elem = TableElement(self, doc_element)
            elif element_type == DocElementType.page_break:
                elem = PageBreakElement(self, doc_element)
            if elem and container:
                if container.is_visible():
                    if elem.x < 0:
                        self.errors.append(Error('errorMsgInvalidPosition', object_id=elem.id, field='position'))
                    elif elem.x + elem.width > container.width:
                        self.errors.append(Error('errorMsgInvalidSize', object_id=elem.id, field='position'))
                    if elem.y < 0:
                        self.errors.append(Error('errorMsgInvalidPosition', object_id=elem.id, field='position'))
                    elif elem.y + elem.height > container.height:
                        self.errors.append(Error('errorMsgInvalidSize', object_id=elem.id, field='position'))
                container.add(elem)

        self.context = Context(self, self.parameters, data)

        computed_parameters = []
        self.process_data(self.data, parameter_list, is_test_data, computed_parameters, parents=[])
        try:
            if not self.errors:
                self.compute_parameters(computed_parameters, self.data)
        except ReportBroError:
            pass

    def generate_pdf(self, filename='', add_watermark=False):
        renderer = DocumentPDFRenderer(header_band=self.header,
                content_band=self.content, footer_band=self.footer,
                report=self, context=self.context,
                additional_fonts=self.additional_fonts,
                filename=filename, add_watermark=add_watermark)
        return renderer.render()

    def generate_xlsx(self, filename=''):
        renderer = DocumentXLSXRenderer(header_band=self.header, content_band=self.content, footer_band=self.footer,
                report=self, context=self.context, filename=filename)
        return renderer.render()

    # goes through all elements in header, content and footer and throws a ReportBroError in case
    # an element is invalid
    def verify(self):
        if self.document_properties.header_display != BandDisplay.never:
            self.header.prepare(self.context, only_verify=True)
        self.content.prepare(self.context, only_verify=True)
        if self.document_properties.header_display != BandDisplay.never:
            self.footer.prepare(self.context, only_verify=True)


    def process_data(self, data, parameters, is_test_data, computed_parameters, parents):
        field = 'test_data' if is_test_data else 'type'
        for parameter in parameters:
            if parameter.is_internal:
                continue
            if regex_valid_identifier.match(parameter.name) is None:
                self.errors.append(Error('errorMsgInvalidParameterName', object_id=parameter.id, field='name'))
            parameter_type = parameter.type
            if parameter_type in (ParameterType.average, ParameterType.sum) or parameter.eval:
                if not parameter.expression:
                    self.errors.append(Error('errorMsgMissingExpression', object_id=parameter.id, field='expression'))
                else:
                    parent_names = []
                    for parent in parents:
                        parent_names.append(parent.name)
                    computed_parameters.append(dict(parameter=parameter, parent_names=parent_names))
            else:
                value = data.get(parameter.name)
                if value is None and not is_test_data:
                    self.errors.append(Error('errorMsgMissingData', object_id=parameter.id, field=field))
                else:
                    if parameter_type == ParameterType.string:
                        if value is None:
                            value = ''
                        if not isinstance(value, basestring):
                            raise RuntimeError('value of parameter {name} must be str type (unicode for Python 2.7.x)'.
                                    format(name=parameter.name))
                    elif parameter_type == ParameterType.number:
                        if value:
                            if isinstance(value, basestring):
                                value = value.replace(',', '.')
                            try:
                                value = decimal.Decimal(str(value))
                            except (decimal.InvalidOperation, TypeError):
                                self.errors.append(Error('errorMsgInvalidNumber', object_id=parameter.id, field=field))
                        else:
                            value = decimal.Decimal('0')
                    elif parameter_type == ParameterType.date:
                        if not value and is_test_data:
                            value = datetime.datetime.now()
                        elif isinstance(value, basestring):
                            try:
                                format = '%Y-%m-%d'
                                colon_count = value.count(':')
                                if colon_count == 1:
                                    format = '%Y-%m-%d %H:%M'
                                elif colon_count == 2:
                                    format = '%Y-%m-%d %H:%M:%S'
                                value = datetime.datetime.strptime(value, format)
                            except (ValueError, TypeError):
                                self.errors.append(Error('errorMsgInvalidDate', object_id=parameter.id, field=field))
                        elif isinstance(value, datetime.date):
                            value = datetime.datetime(value.year, value.month, value.day)
                        elif not isinstance(value, datetime.datetime):
                            self.errors.append(Error('errorMsgInvalidDate', object_id=parameter.id, field=field))
                    elif not parents:
                        if parameter_type == ParameterType.array:
                            if isinstance(value, list):
                                parents.append(parameter)
                                parameter_list = list(parameter.fields.values())
                                for row in value:
                                    self.process_data(row, parameter_list, is_test_data, computed_parameters,
                                        parents=parents)
                                parents = parents[:-1]
                            else:
                                error_object_id = parents[-1].id if parents else parameter.id
                                self.errors.append(Error('errorMsgInvalidArray',
                                        object_id=error_object_id, field=field))
                        elif parameter_type == ParameterType.map:
                            if isinstance(value, dict):
                                if isinstance(parameter.children, list):
                                    parents.append(parameter)
                                    self.process_data(value, parameter.children, is_test_data, computed_parameters,
                                        parents=parents)
                                    parents = parents[:-1]
                                else:
                                    self.errors.append(Error('errorMsgInvalidMap', object_id=parameter.id, field='type'))
                            else:
                                self.errors.append(Error('errorMsgMissingData', object_id=parameter.id, field='name'))
                    data[parameter.name] = value

    def compute_parameters(self, computed_parameters, data):
        for computed_parameter in computed_parameters:
            parameter = computed_parameter['parameter']
            value = None
            if parameter.type in (ParameterType.average, ParameterType.sum):
                expr = Context.strip_parameter_name(parameter.expression)
                pos = expr.find('.')
                if pos == -1:
                    self.errors.append(Error('errorMsgInvalidAvgSumExpression',
                            object_id=parameter.id, field='expression'))
                else:
                    parameter_name = expr[:pos]
                    parameter_field = expr[pos+1:]
                    items = data.get(parameter_name)
                    if not isinstance(items, list):
                        self.errors.append(Error('errorMsgInvalidAvgSumExpression',
                                object_id=parameter.id, field='expression'))
                    else:
                        total = decimal.Decimal(0)
                        for item in items:
                            item_value = item.get(parameter_field)
                            if item_value is None:
                                self.errors.append(Error('errorMsgInvalidAvgSumExpression',
                                        object_id=parameter.id, field='expression'))
                                break
                            total += item_value
                        if parameter.type == ParameterType.average:
                            value = total / len(items)
                        elif parameter.type == ParameterType.sum:
                            value = total
            else:
                value = self.context.evaluate_expression(parameter.expression, parameter.id, field='expression')

            data_entry = data
            valid = True
            for parent_name in computed_parameter['parent_names']:
                data_entry = data_entry.get(parent_name)
                if not isinstance(data_entry, dict):
                    self.errors.append(Error('errorMsgInvalidParameterData',
                            object_id=parameter.id, field='name'))
                    valid = False
            if valid:
                data_entry[parameter.name] = value