"""Routes du portail personnel et HR Assistance ; les actions métier restent dans les modèles."""

import base64
from uuid import uuid4

from odoo import http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import content_disposition, request


class HrRequestPortal(http.Controller):

    def _is_portal_user(self):
        return request.env.user.has_group('base.group_portal')

    def _is_hr_assistance(self):
        return request.env.user.has_group(
            'hr_requests.group_hr_request_hr_assistance'
        )

    def _get_portal_employee(self):
        return request.env['hr.request']._get_employee_for_user(
            request.env.user, strict=False
        )

    def _get_managed_employees(self):
        return request.env['hr.request']._get_hr_assistance_employees()

    def _get_employees_page_records(self):
        # Personal requests belong exclusively on the personal page.
        """Afficher les employés affectés hors demandeur personnel dans la page employés."""
        return (request.env.user.sudo().managed_employee_ids
                - self._get_portal_employee()).sorted('name')

    def _portal_page_values(self, managed_page):
        """Partager les liens et le contexte de navigation entre les pages du portail."""
        return {
            'managed_page': managed_page,
            'is_hr_assistance': self._is_hr_assistance(),
            'portal_list_url': '/my/hr-requests/employees' if managed_page else '/my/hr-requests',
            'portal_new_url': '/my/hr-requests/employees/new' if managed_page else '/my/hr-requests/new',
        }

    @http.route(
        ['/my/hr-requests', '/my/hr-requests/employees'],
        type='http',
        auth='user',
        website=True,
    )
    def portal_my_hr_requests(self, **kwargs):
        """Chercher les demandes avec les droits courants avant de lire les libellés en sudo."""
        if not self._is_portal_user():
            return request.redirect('/odoo')

        managed_page = request.httprequest.path.rstrip('/').endswith('/employees')
        if managed_page and not self._is_hr_assistance():
            return request.not_found()
        employee = self._get_portal_employee()
        if managed_page:
            domain = [('requester_employee_id', 'in', self._get_employees_page_records().ids)]
        else:
            if not employee:
                return request.render('hr_requests.portal_hr_request_no_employee')
            domain = [('requester_employee_id', '=', employee.id)]

        hr_requests = request.env['hr.request'].search(
            domain,
            order='create_date desc',
        ).sudo()

        return request.render(
            'hr_requests.portal_my_hr_requests',
            {
                'employee': employee,
                'hr_requests': hr_requests,
                **self._portal_page_values(managed_page),
                'page_name': 'hr_requests',
            },
        )

    @http.route(
        ['/my/hr-requests/new', '/my/hr-requests/employees/new'],
        type='http',
        auth='user',
        website=True,
        methods=['GET', 'POST'],
    )
    def portal_new_hr_request(self, **post):
        """Valider les choix du formulaire puis déléguer la soumission idempotente au modèle.

        Le savepoint annule création et soumission ensemble en cas d’erreur métier,
        tout en conservant les valeurs saisies pour réafficher le formulaire.
        """
        if not self._is_portal_user():
            return request.redirect('/odoo')

        managed_page = request.httprequest.path.rstrip('/').endswith('/employees/new')
        if managed_page and not self._is_hr_assistance():
            return request.not_found()
        employee = self._get_portal_employee()
        managed_employees = self._get_employees_page_records() if managed_page else request.env['hr.employee']
        if not managed_page and not employee:
            return request.render('hr_requests.portal_hr_request_no_employee')

        request_types = request.env['hr.request.type'].search(
            [('active', '=', True)],
            order='sequence, name',
        )

        values = {
            'employee': employee,
            'managed_employees': managed_employees,
            **self._portal_page_values(managed_page),
            'request_types': request_types,
            'page_name': 'hr_request_new',
            'error': False,
            'selected_requester_employee_id': False,
            'selected_request_type_id': False,
            'description': '',
            'submission_token': post.get('submission_token') if request.httprequest.method == 'POST' else str(uuid4()),
        }

        if managed_page and not managed_employees:
            values['error'] = (
                request.env._('No employee is assigned to you.')
            )

        if request.httprequest.method == 'POST':
            request_type_value = post.get('request_type_id')
            description = (post.get('description') or '').strip()
            values['description'] = description

            if managed_page:
                requester_value = post.get('requester_employee_id')

                try:
                    requester_id = int(requester_value)
                except (TypeError, ValueError):
                    values['error'] = request.env._('Please select a valid employee.')
                    return request.render(
                        'hr_requests.portal_new_hr_request',
                        values,
                    )

                values['selected_requester_employee_id'] = requester_id
                selected_employee = self._get_employees_page_records().filtered(
                    lambda employee: employee.id == requester_id
                )[:1]

                if not selected_employee:
                    values['error'] = (
                        request.env._('Please select an employee assigned to you.')
                    )
                    return request.render(
                        'hr_requests.portal_new_hr_request',
                        values,
                    )
            else:
                selected_employee = employee

            try:
                request_type_id = int(request_type_value)
            except (TypeError, ValueError):
                values['error'] = request.env._('Please select a valid request type.')
                return request.render(
                    'hr_requests.portal_new_hr_request',
                    values,
                )

            values['selected_request_type_id'] = request_type_id

            request_type = request.env['hr.request.type'].search(
                [
                    ('id', '=', request_type_id),
                    ('active', '=', True),
                ],
                limit=1,
            )

            if not request_type:
                values['error'] = (
                    request.env._('The selected request type is not available.')
                )
                return request.render(
                    'hr_requests.portal_new_hr_request',
                    values,
                )

            try:
                with request.env.cr.savepoint():
                    hr_request = request.env['hr.request']._create_portal_submission(values['submission_token'], {
                        'requester_employee_id': selected_employee.id,
                        'request_type_id': request_type.id,
                        'description': description,
                    })


            except (
                AccessError,
                UserError,
                ValidationError,
            ) as error:
                values['error'] = str(error)
                return request.render(
                    'hr_requests.portal_new_hr_request',
                    values,
                )

            return request.redirect(
                f'/my/hr-requests/{hr_request.id}'
            )

        return request.render(
            'hr_requests.portal_new_hr_request',
            values,
        )

    @http.route(
        '/my/hr-requests/<int:request_id>',
        type='http',
        auth='user',
        website=True,
    )
    def portal_hr_request_detail(self, request_id, **kwargs):
        """Contrôler le périmètre de la demande avant toute lecture privilégiée de ses détails."""
        if not self._is_portal_user():
            return request.redirect('/odoo')

        if not self._is_hr_assistance() and not self._get_portal_employee():
            return request.render(
                'hr_requests.portal_hr_request_no_employee'
            )

        if self._is_hr_assistance():
            managed_ids = self._get_managed_employees().ids
            domain = [
                ('id', '=', request_id),
                ('requester_employee_id', 'in', managed_ids),
            ]
        else:
            portal_employee = self._get_portal_employee()
            domain = [
                ('id', '=', request_id),
                ('requester_employee_id', '=', portal_employee.id),
            ]

        hr_request = request.env['hr.request'].search(
            domain,
            limit=1,
        )

        if not hr_request:
            return request.not_found()

        visible_document = hr_request._get_generated_attachment()
        hr_request = hr_request.sudo()
        employee = hr_request.requester_employee_id
        attachment_name = False

        if visible_document:
            attachment_name = visible_document.name

        department_name = (
            employee.department_id.name
            if employee.department_id
            else '-'
        )

        error = request.session.pop(
            'hr_request_error',
            False,
        )

        return request.render(
            'hr_requests.portal_hr_request_detail',
            {
                'employee': employee,
                'hr_request': hr_request,
                **self._portal_page_values(
                    self._is_hr_assistance() and employee != self._get_portal_employee()
                ),
                'department_name': department_name,
                'attachment_name': attachment_name,
                'page_name': 'hr_request_detail',
                'error': error,
            },
        )

    @http.route(
        '/my/hr-requests/<int:request_id>/submit',
        type='http',
        auth='user',
        website=True,
        methods=['POST'],
    )
    def portal_submit_hr_request(self, request_id, **post):
        """Soumettre un brouillon autorisé par POST et réafficher les erreurs métier."""
        if not self._is_portal_user():
            return request.redirect('/odoo')

        if not self._is_hr_assistance() and not self._get_portal_employee():
            return request.render(
                'hr_requests.portal_hr_request_no_employee'
            )

        if self._is_hr_assistance():
            managed_ids = self._get_managed_employees().ids
            domain = [
                ('id', '=', request_id),
                ('state', '=', 'draft'),
                ('requester_employee_id', 'in', managed_ids),
            ]
        else:
            portal_employee = self._get_portal_employee()
            domain = [
                ('id', '=', request_id),
                ('state', '=', 'draft'),
                ('requester_employee_id', '=', portal_employee.id),
            ]

        hr_request = request.env['hr.request'].search(
            domain,
            limit=1,
        )

        if not hr_request:
            return request.not_found()

        try:
            with request.env.cr.savepoint():
                hr_request.action_submit()

        except (
            AccessError,
            UserError,
            ValidationError,
        ) as error:
            request.session['hr_request_error'] = str(error)
            return request.redirect(
                f'/my/hr-requests/{request_id}'
            )

        return request.redirect(
            f'/my/hr-requests/{request_id}'
        )

    @http.route(
        '/my/hr-requests/<int:request_id>/download',
        type='http',
        auth='user',
        website=True,
    )
    def portal_download_hr_document(self, request_id, **kwargs):
        """Vérifier demande et pièce jointe avant de retourner le contenu du document."""
        if not self._is_portal_user():
            return request.redirect('/odoo')

        if self._is_hr_assistance():
            managed_ids = self._get_managed_employees().ids
            domain = [
                ('id', '=', request_id),
                ('requester_employee_id', 'in', managed_ids),
            ]
        else:
            portal_employee = self._get_portal_employee()
            if not portal_employee:
                return request.not_found()
            domain = [
                ('id', '=', request_id),
                ('requester_employee_id', '=', portal_employee.id),
            ]

        hr_request = request.env['hr.request'].search(
            domain,
            limit=1,
        )

        if (
            not hr_request
        ):
            return request.not_found()

        attachment = hr_request._get_generated_attachment()
        if not attachment:
            return request.not_found()
        file_content = base64.b64decode(
            attachment.datas or b''
        )

        return request.make_response(
            file_content,
            headers=[
                (
                    'Content-Type',
                    attachment.mimetype
                    or 'application/octet-stream',
                ),
                (
                    'Content-Disposition',
                    content_disposition(attachment.name),
                ),
            ],
        )