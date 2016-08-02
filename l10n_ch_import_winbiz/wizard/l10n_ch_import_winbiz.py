# -*- coding: utf-8 -*-
# Copyright 2015 Camptocamp SA
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import base64
from xlrd import open_workbook, xldate_as_tuple
import tempfile
from openerp import models, fields, api, exceptions
from openerp.tools.translate import _
import datetime


class AccountWinbizImport(models.TransientModel):
    _name = 'account.winbiz.import'
    _description = 'Import Accounting Winbiz'
    _rec_name = 'state'

    company_id = fields.Many2one('res.company', 'Company', invisible=True)
    report = fields.Text('Report', readonly=True)
    state = fields.Selection(selection=[
        ('draft', "Draft"),
        ('done', "Done"),
        ('error', "Error")],
        readonly=True,
        default='draft')
    file = fields.Binary('File', required=True)
    imported_move_ids = fields.Many2many(
        'account.move', 'import_winbiz_move_rel',
        string='Imported moves')

    @api.multi
    def open_account_moves(self):
        res = {
                'domain': str([('id', 'in', self.imported_move_ids.ids)]),
                'name': 'Account Move',
                'view_type': 'form',
                'view_mode': 'tree,form',
                'res_model': 'account.move',
                'view_id': False,
                'type': 'ir.actions.act_window',
                }
        return res

    def _parse_xls(self):
        """Parse stored Excel file.

        Manage base 64 decoding.

        :param imp_id: current importer id
        :returns: generator

        """
        # We use tempfile in order to avoid memory error with large files
        with tempfile.TemporaryFile() as src:
            content = self.file
            src.write(content)
            with tempfile.NamedTemporaryFile() as decoded:
                src.seek(0)
                base64.decode(src, decoded)
                decoded.seek(0)
                wb = open_workbook(decoded.name, encoding_override='cp1252')
                self.wb = wb
                sheet = wb.sheet_by_index(0)

                def vals(n):
                    return [c.value for c in sheet.row(n)]
                head = vals(0)
                res = [dict(zip(head, vals(i))) for i in xrange(1, sheet.nrows)]
                res.sort(key=lambda e: e[u'numéro'])
                return res

    def _parse_date(self, date):
        """Parse a date coming from Excel.

           :param date: cell value
           :returns: datetime.date
        """
        if isinstance(date, basestring):
            from babel import Locale
            d, m, y = date.split('-')
            d = int(d)
            mapping = Locale('en').months['format']['abbreviated']
            mapping = dict(zip(mapping.values(), mapping.keys()))
            m = mapping[m]
            y = 2000 + int(y)
            return datetime.datetime(y, m, d)
        else:
            return datetime.datetime(*xldate_as_tuple(date, self.wb.datemode))

    class LineIntermediate(object):
        '''has a single ``amount`` attribute that's negative for debit, but can
        be converted into a dict for the ORM ``create()`` with ``dict(self)``
        '''
        def __init__(self, name, account, amount=0, tax=None, originator_tax=None):
            self.name = name
            self.account = account
            self.amount = amount
            self.tax = tax
            self.originator_tax = originator_tax
        def __iter__(self):
            yield 'name', self.name
            yield 'account_id', self.account.id
            if self.amount < 0:
                yield 'debit', -self.amount
            else:
                yield 'credit', self.amount
            if self.tax is not None:
                yield 'tax_ids', [(4, self.tax.id, 0)]
            if self.originator_tax is not None:
                yield 'tax_line_id', self.originator_tax.id

    @api.multi
    def _standardise_data(self, data):
        """
        This function split one line of the spreadsheet into multiple lines.
        Winbiz just writes one line per move.
        """

        tax_obj = self.env['account.tax']
        journal_obj = self.env['account.journal']
        account_obj = self.env['account.account']

        def find_account(code):
            res = account_obj.search([('code',  '=', code)], limit=1)
            if not res:
                raise exceptions.MissingError(
                    _("No account with code %s") % code)
            return res

        def find_journal(winbiz_code):
            mapping = {
                'a': 'BILL',
                'd': 'MISC',
                'i': 'STJ',
                'm': 'MISC',
                'o': 'OJ',
                's': 'JS',
                'v': 'INV',
                }
            code = mapping[winbiz_code]
            return journal_obj.search([('code', '=', code)], limit=1)

        def prepare_move(lines, journal, date, ref):
            return {'line_ids': [(0, 0, dict(ln)) for ln in lines],
                    'journal_id': journal.id,
                    'date': date,
                    'ref': ref}

        prepare_line = self.LineIntermediate

        # loop
        incomplete = None
        previous_pce = None
        previous_date = None
        previous_journal = None
        previous_tax = None
        lines = []
        for self.index, winbiz_item in enumerate(data, 1):
            if previous_pce not in (None, winbiz_item[u'pièce']):
                yield prepare_move(lines, previous_journal, previous_date,
                                   ref=previous_pce)
                lines = []
                incomplete = None
            previous_pce = winbiz_item[u'pièce']
            previous_date = self._parse_date(winbiz_item[u'date'])
            previous_journal = find_journal(winbiz_item[u'journal'])

            if winbiz_item['ecr_tvatx'] != 0.0:
                if winbiz_item['ecr_tvadc'] == 'c':
                    scope = 'sale'
                else:
                    assert winbiz_item['ecr_tvadc'] == 'd'
                    scope = 'purchase'
                tax = tax_obj.search([('amount', '=', winbiz_item['ecr_tvatx']),
                                     ('type_tax_use', '=', scope)], limit=1)
                if not tax:
                    raise exceptions.MissingError("No tax found with amount = %r and type = %r" % (winbiz_item['ecr_tvatx'], scope))
            else:
                tax = None
            if int(winbiz_item['ecr_tvatyp']) < 0:
                assert previous_tax is not None
                originator_tax = previous_tax
            else:
                originator_tax = None
            previous_tax = tax

            amount = float(winbiz_item[u'montant'])
            recto_line = verso_line = None
            if winbiz_item[u'cpt_débit'] != 'Multiple':
                account = find_account(winbiz_item[u'cpt_débit'])
                if incomplete is not None and incomplete.account == account:
                    incomplete.amount -= amount
                else:
                    recto_line = prepare_line(
                            name=winbiz_item[u'libellé'],
                            amount=(-amount),
                            account=account,
                            tax=tax,
                            originator_tax=originator_tax)
                    lines.append(recto_line)

            if winbiz_item[u'cpt_crédit'] != 'Multiple':
                account = find_account(winbiz_item[u'cpt_crédit'])
                if incomplete is not None and incomplete.account == account:
                    incomplete.amount += amount
                else:
                    verso_line = prepare_line(
                            name=winbiz_item[u'libellé'],
                            amount=amount,
                            account=account,
                            tax=tax,
                            originator_tax=originator_tax)
                    lines.append(verso_line)

            if winbiz_item[u'cpt_débit'] == 'Multiple':
                assert incomplete is None
                incomplete = verso_line
            if winbiz_item[u'cpt_crédit'] == 'Multiple':
                assert incomplete is None
                incomplete = recto_line

        yield prepare_move(lines, previous_journal, previous_date,
                           ref=previous_pce)

    @api.multi
    def _import_file(self):
        self.index = None
        move_obj = self.env['account.move'].with_context(dont_create_taxes=True)
        data = self._parse_xls()
        data = self._standardise_data(data)
        self.imported_move_ids = [move_obj.create(mv).id for mv in data]

    @api.multi
    def import_file(self):
        try:
            self._import_file()
        except Exception as exc:
            self.env.cr.rollback()
            self.write({
                'state': 'error',
                'report': 'Error (at row %s):\n%r' % (self.index, exc)})
            return {'name': _('Accounting WinBIZ Import'),
                    'type': 'ir.actions.act_window',
                    'res_model': 'account.winbiz.import',
                    'res_id': self.id,
                    'view_type': 'form',
                    'view_mode': 'form',
                    'target': 'new'}
        self.state = 'done'
        # show the resulting moves in main content area
        return {'domain': str([('id', 'in', self.imported_move_ids.ids)]),
                'name': _('Imported Journal Entries'),
                'view_type': 'form',
                'view_mode': 'tree,form',
                'res_model': 'account.move',
                'view_id': False,
                'type': 'ir.actions.act_window'}
