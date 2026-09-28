"""Validation du PDF signé et transition atomique vers l’état signé."""

import base64
import binascii
import io

from odoo import fields, models

from odoo.exceptions import UserError
from odoo.tools.pdf import PdfReader


class HrRequestSignWizard(models.TransientModel):
    _name = 'hr.request.sign.wizard'
    _description = 'Upload Signed Document'

    request_id = fields.Many2one('hr.request', required=True, readonly=True)
    document = fields.Binary(string='Signed PDF', required=True, attachment=False)
    filename = fields.Char(string='Filename')

    def action_confirm(self):
        """Contrôler le gestionnaire et le PDF avant de joindre le document signé.

        Le verrou de ligne empêche deux traitements simultanés de remplacer un PDF
        déjà accepté. Le contenu doit être un PDF non chiffré de 20 Mo au maximum.
        """
        self.ensure_one()
        record = self.request_id
        record._check_manager()
        # Serialise with completion and another upload; never overwrite an accepted signed PDF.
        self.env.cr.execute('SELECT id FROM hr_request WHERE id = %s FOR UPDATE', (record.id,))
        record.invalidate_recordset(['state'])
        if record.state != 'submitted':
            raise UserError(self.env._('Only a submitted request can receive a signed document.'))
        try:
            content = base64.b64decode(self.document or b'', validate=True)
        except (ValueError, binascii.Error) as error:
            raise UserError(self.env._('Please upload a valid PDF document.')) from error
        if len(content) > 20 * 1024 * 1024:
            raise UserError(self.env._('The signed PDF must not exceed 20 MB.'))
        try:
            pdf = PdfReader(io.BytesIO(content), strict=False)
            if not content.startswith(b'%PDF-') or pdf.is_encrypted or not len(pdf.pages):
                raise ValueError('Invalid or encrypted PDF')
        except Exception as error:
            raise UserError(self.env._('Please upload a valid, unencrypted PDF document.')) from error
        attachment = self.env['ir.attachment'].sudo().create({
            'name': '%s - signé.pdf' % record.name, 'datas': self.document,
            'mimetype': 'application/pdf', 'res_model': 'hr.request', 'res_id': record.id,
        })
        record._workflow_write({
            'state': 'signed', 'signed_document_id': attachment.id,
            'signed_date': fields.Datetime.now(), 'signed_by_id': self.env.uid,
        })
        record._post_note(self.env._('Signed document uploaded.'))
        return {'type': 'ir.actions.act_window_close'}
