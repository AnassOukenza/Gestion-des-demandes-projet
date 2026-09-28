"""Types de demande et correspondance avec les modèles de documents."""

import re
import unicodedata

from odoo import api, fields, models


class HrRequestType(models.Model):
    _name = 'hr.request.type'
    _description = 'HR Request Type'
    _order = 'sequence, name'

    name = fields.Char(
        string='Name',
        required=True,
        translate=True,
    )

    code = fields.Char(
        string='Code',
        required=True,
        readonly=True,
        copy=False
    )

    sequence = fields.Integer(
        string='Sequence',
        default=10
    )

    active = fields.Boolean(
        string='Active',
        default=True
    )

    def _generate_code(self, name):
        """Normaliser le libellé en code technique puis reconnaître les types standard."""
        normalized_name = unicodedata.normalize(
            'NFKD',
            name
        ).encode(
            'ascii',
            'ignore'
        ).decode()

        code = re.sub(
            r'[^a-zA-Z0-9]+',
            '_',
            normalized_name
        )

        code = code.strip('_').lower()
        return self._canonical_document_code(code)

    @api.model
    def _canonical_document_code(self, code):
        """Faire correspondre les anciens codes français aux clés des rapports existants."""
        return {
            'attestation_de_travail': 'work_certificate',
            'attestation_de_salaire': 'salary_certificate',
            'demande_de_credit': 'credit_request',
        }.get(code, code)

    def _document_code(self):
        self.ensure_one()
        return self._canonical_document_code(self.code)


    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('code'):
                vals['code'] = self._generate_code(
                    vals.get('name', '')
                )

        records = super().create(vals_list)
        records._seed_french_names()
        return records

    def _seed_french_names(self):
        """Traduire les noms standard sans remplacer les libellés personnalisés."""
        labels = {
            'work_certificate': ('Work Certificate', 'Attestation de travail'),
            'salary_certificate': ('Salary Certificate', 'Attestation de salaire'),
            'credit_request': ('Credit Request', 'Demande de crédit'),
        }
        languages = self.env['res.lang'].sudo().search([
            ('active', '=', True),
            ('code', '=like', 'fr_%'),
        ]).mapped('code')
        for record in self:
            pair = labels.get(record.code)
            if not pair or record.with_context(lang='en_US').name != pair[0]:
                continue
            for lang in languages:
                localized = record.with_context(lang=lang)
                if localized.name == pair[0]:
                    localized.write({'name': pair[1]})
        return True

    @api.model
    def _translate_existing_french_names(self):
        """Appliquer les traductions aux types existants, y compris archivés."""
        records = self.sudo().with_context(active_test=False).search([
            ('code', 'in', ['work_certificate', 'salary_certificate', 'credit_request']),
        ])
        return records._seed_french_names()
