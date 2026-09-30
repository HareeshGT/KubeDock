from PyQt5.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QMessageBox,
    QShortcut,
    QProgressBar,
    QComboBox,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QGraphicsBlurEffect

from ui_icons import set_icon, apply_text_icon, icon_button
from themes import T, load_settings, save_settings
from utils import monospace_font
from .core import *


class K8sAIOpsOperationsMixin:
    def _submit(self):
        if not self._ai_access_allowed:
            self.refresh_ai_access()
            return
        if self._busy:
            return
        request = self.request_input.text().strip()
        if not request:
            return
        if len(request) > MAX_PROMPT_CHARS:
            self._write_error(f'Request is too long. Maximum is {MAX_PROMPT_CHARS} characters.')
            return
        namespace = self._namespace()
        local_response = _general_question_response(request, namespace)
        if local_response is not None:
            self.output.append(f"""<br><span style="color:{T['ACCENT']}; font-weight:700;">Assistant:</span><br><span style="color:{T['TEXT_PRIMARY']}">{self._escape_html(local_response).replace(chr(10), '<br>')}</span>""")
            self.status_lbl.setText('Ready')
            self.request_input.setFocus()
            return
        if not self.ssh:
            self._write_error('No Kubernetes SSH connection is active.')
            return
        # "delete that pod" / "delete it" / "delete that <name> pod": resolve from
        # the previous lookup locally instead of asking the AI, which tends to
        # answer with a clarification because the earlier search had no namespace.
        followup_context = self._kube_context()
        followup = resolve_followup_delete(request, self._history, followup_context) if followup_context else None
        if followup is not None:
            self.output.append(f"""<br><span style="color:{T['ACCENT2']}">$ {self._escape_html(request)}</span>""")
            if followup.get('clarify'):
                self._write_info('\n' + followup['clarify'])
                self._speak('I need more information to do that.')
                self.request_input.setFocus()
                return
            self._write_info('\n' + followup['note'])
            self._set_busy(True)
            self._execute_action(followup['action'])
            return
        provider = ai_assist.get_provider()
        api_key = ai_assist.get_api_key(provider)
        if not api_key:
            label = ai_assist.PROVIDERS.get(provider, {}).get('label', provider)
            QMessageBox.information(self, 'No API key set', f'Add a {label} API key in Settings → AI to use AI Kubernetes Operations.')
            return
        context = self._context()
        kube_context = self._kube_context()
        if not kube_context:
            self._write_error('No Kubernetes context is selected. Refresh contexts and try again.')
            return
        history_for_context = [row for row in self._history if not row.get('context') or row.get('context') == kube_context]
        self.output.append(f"""<br><span style="color:{T['ACCENT2']}">$ {self._escape_html(request)}</span>""")
        self.output.append(f"""<span style="color:{T['TEXT_MUTED']}">Namespace: {self._escape_html(namespace or 'all namespaces')}</span>""")
        self._set_busy(True)
        self._speak('Processing your request.')
        worker = K8sAIInterpretWorker(provider, api_key, ai_assist.get_model(provider), request, namespace, context, history_for_context)
        worker.done.connect(self._on_ai_done)
        worker.error.connect(self._on_ai_error)
        worker.finished.connect(self._on_ai_finished)
        self._ai_worker = worker
        track_worker(self._workers, worker)
        worker.start()

    def _on_ai_done(self, text: str):
        try:
            raw_action = _extract_json(text)
            action = validate_action(raw_action)
        except Exception as exc:
            self._write_error(f'AI interpretation failed: {exc}')
            self._speak("Sorry, I couldn't understand that request.")
            return
        if action['kind'] == 'clarification':
            self._write_info('\nAI needs more information:\n' + action['reason'])
            self._speak('I need more information to do that.')
            self.request_input.setFocus()
            return
        if action['kind'] == 'unsupported':
            self._write_error('\n' + action['reason'])
            self._speak("That operation isn't supported.")
            return
        if action.get('action') == 'get' and (action.get('key') or action.get('keys')) and (not action.get('namespace')):
            reason = f"Please specify the Kubernetes namespace to read keys from this {action.get('resource')}."
            self._write_info('\nAI needs more information:\n' + reason)
            self._speak('I need the Kubernetes namespace to do that.')
            self.request_input.setFocus()
            return
        if action.get('action') in {'scale', 'restart', 'delete'} and (not action.get('namespace')):
            # The AI (or the user) gave a target but no namespace. If an earlier
            # successful lookup located this exact resource in exactly one
            # namespace, use that instead of asking again. Ambiguous or unknown
            # targets fall through to the clarification below.
            target = resolve_target_from_history(action, self._history, self._kube_context())
            if target:
                action['namespace'], action['name'] = target
                self._write_info(f"\nUsing {action.get('resource')} {action['name']} in namespace \"{action['namespace']}\" from the earlier lookup.")
        if action.get('action') in {'scale', 'restart', 'delete'} and (not action.get('namespace')):
            reason = f"Please specify the Kubernetes namespace for this {action.get('action')} operation."
            self._write_info('\nAI needs more information:\n' + reason)
            self._speak('I need the Kubernetes namespace to do that safely.')
            self.request_input.setFocus()
            return
        self._execute_action(action)

    def _on_ai_error(self, message: str):
        """Show AI/provider errors in a dialog and keep a compact log entry."""
        message = str(message or 'Unknown AI error').strip()
        self._write_error(f'\nAI error:\n{message}')
        self._speak('The AI request failed.')
        lower = message.lower()
        if '429' in lower or 'quota' in lower or 'rate limit' in lower:
            title = 'AI API Quota Exceeded'
        elif '401' in lower or 'unauthorized' in lower or 'invalid api key' in lower:
            title = 'AI API Authentication Error'
        elif '403' in lower or 'forbidden' in lower:
            title = 'AI API Access Denied'
        elif 'timeout' in lower:
            title = 'AI API Timeout'
        elif 'connection' in lower or 'network' in lower:
            title = 'AI API Connection Error'
        else:
            title = 'AI API Error'
        dlg = QMessageBox(self)
        dlg.setIcon(QMessageBox.Warning)
        dlg.setWindowTitle(title)
        dlg.setText(title)
        dlg.setInformativeText('The AI request could not be completed.')
        dlg.setDetailedText(message)
        dlg.setStyleSheet(f'\n      QMessageBox {{\n        min-width: 520px;\n      }}\n      ')
        dlg.exec_()

    def _on_ai_finished(self):
        self._ai_worker = None
        if self._operation_worker is None:
            self._set_busy(False)

    def _execute_action(self, action: dict):
        kube_context = self._kube_context()
        if not kube_context:
            self._write_error('No Kubernetes context is selected; operation blocked for safety.')
            self._set_busy(False)
            return
        action['context'] = kube_context
        description = operation_description(action)
        is_relative_scale = action['action'] == 'scale' and action.get('mode') == 'relative'
        if is_relative_scale:
            action['_scale_direction'] = 'up' if action.get('delta', 0) >= 0 else 'down'
            self.output.append(f"""<br><span style="color:{T['ACCENT']}; font-weight:700;">AI interpreted:</span><br><span style="color:{T['TEXT_PRIMARY']}">{self._escape_html(description)}</span><br><span style="color:{T['TEXT_DIM']}">Reading current replica count before applying the change…</span>""")
            command = None
        else:
            command = build_kubectl_command(action)
            self.output.append(f"""<br><span style="color:{T['ACCENT']}; font-weight:700;">AI interpreted:</span><br><span style="color:{T['TEXT_PRIMARY']}">{self._escape_html(description)}</span><br><span style="color:{T['TEXT_DIM']}">Command: {self._escape_html(command)}</span>""")
            self._speak('Command build completed.')
        namespace = action.get('namespace', '')
        protected = is_protected_namespace(namespace)
        risk = _risk_level(action, kube_context, protected)
        needs_confirmation = action['action'] in {'delete', 'restart'}
        prod_context = any((x in kube_context.lower() for x in ('prod', 'production')))
        confirmed = False
        if action['action'] == 'scale' and (protected or prod_context):
            needs_confirmation = True
        if risk in {'high', 'critical'}:
            needs_confirmation = True
        if needs_confirmation:
            if action['action'] == 'delete':
                title = 'Confirm Kubernetes Delete'
            elif action['action'] == 'restart':
                title = 'Confirm Kubernetes Restart'
            else:
                title = 'Confirm Kubernetes Scale — Protected Namespace'
            extra_note = ''
            if any((x in kube_context.lower() for x in ('prod', 'production'))):
                extra_note += '\n\nSelected context looks like a production cluster.'
            if protected:
                extra_note += f'\n\n "{namespace}" matches a protected namespace pattern configured in Settings.'
            if action['action'] == 'restart':
                extra_note += '\n\nThis restarts every pod in the workload.'
            answer = QMessageBox.question(self, title, f'{description}\n\nThis operation will modify the cluster.{extra_note}\n\nDo you want to continue?', QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                self._write_info('\nOperation cancelled.')
                self._speak('Operation cancelled.')
                self._set_busy(False)
                return
            confirmed = True
        action['_confirmed'] = confirmed
        if action['action'] == 'scale':
            self._pending_scale_action = dict(action)
            self._read_previous_replicas(action, command)
            return
        self._start_kubectl_operation(action, command)

    def _read_previous_replicas(self, action: dict, command: str):
        resource = action['resource']
        name = action['name']
        namespace = action['namespace']
        if not namespace:
            self._pending_scale_action = None
            self._write_error('\n A namespace is required before reading the current replica count for a scale operation.')
            self._speak('A namespace is required for scaling.')
            self._set_busy(False)
            return
        ctx = action.get('context')
        ctx_flag = '--context {} '.format(shlex.quote(str(ctx or ''))) if ctx else ''
        probe = f'kubectl {ctx_flag} -n {shlex.quote(namespace)} get {resource}/{name} -o jsonpath={{.spec.replicas}}'
        self.status_lbl.setText('Reading current replicas…')
        worker = CommandWorker(self.ssh, probe)
        self._scale_previous_worker = worker
        worker._k8s_probe_action = action
        worker.result.connect(self._on_previous_replicas_result)
        worker.error.connect(lambda error: self._on_previous_replicas_error(error))
        worker.finished.connect(lambda: setattr(self, '_scale_previous_worker', None))
        track_worker(self._workers, worker)
        worker.start()

    def _on_previous_replicas_result(self, output: str, err: str, exit_code: int):
        action = self._pending_scale_action
        if action is None:
            return
        if exit_code != 0:
            self._pending_scale_action = None
            self._write_error('\n Could not read the current replica count, so the scale operation was not executed.\n' + (err or output or 'Unknown error').strip())
            self._speak('Operation failed.')
            self._set_busy(False)
            return
        raw = (output or '').strip()
        try:
            previous = int(raw)
        except (TypeError, ValueError):
            previous = None
        action = dict(action)
        action['previous_replicas'] = previous
        self._pending_scale_action = None
        if action.get('mode') == 'relative':
            if previous is None:
                self._write_error('\n Could not determine the current replica count, so the relative scale request could not be resolved.')
                self._speak('Operation failed.')
                self._set_busy(False)
                return
            delta = action.get('delta', 0)
            target = previous + delta
            clamped = max(MIN_REPLICAS, min(MAX_REPLICAS, target))
            self._write_info(f"\nCurrent replicas: {previous}. Requested change: {('+' if delta >= 0 else '')}{delta}. Target replicas: {clamped}." + (f' (clamped from {target})' if clamped != target else ''))
            action['replicas'] = clamped
            action.pop('delta', None)
            action['mode'] = 'absolute'
        command = build_kubectl_command(action)
        self.output.append(f"""<span style="color:{T['TEXT_DIM']}">Command: {self._escape_html(command)}</span>""")
        self._speak('Command build completed.')
        self._start_kubectl_operation(action, command)

    def _on_previous_replicas_error(self, error: str):
        if self._pending_scale_action is None:
            return
        self._pending_scale_action = None
        self._write_error('\n Could not read the current replica count, so the scale operation was not executed.\n' + str(error))
        self._speak('Operation failed.')
        self._set_busy(False)

    def _start_kubectl_operation(self, action: dict, command: str):
        self.status_lbl.setText('Executing kubectl…')
        self._speak(_spoken_start_phrase(action))
        worker = CommandWorker(self.ssh, command + ' 2>&1')
        worker._k8s_action = action
        worker._k8s_command = command
        worker._k8s_confirmed = bool(action.get('_confirmed', False))
        self._operation_worker = worker
        worker.result.connect(lambda output, err, exit_code, a=action, c=command: self._on_operation_result(a, c, output, err, exit_code))
        worker.error.connect(self._on_operation_error)
        worker.finished.connect(self._on_operation_finished)
        track_worker(self._workers, worker)
        worker.start()

    def _on_operation_result(self, action: dict, command: str, output: str, err: str, exit_code: int):
        output = (output or '').strip()
        error_text = (err or '').strip()
        if action.get('action') == 'get' and action.get('keys'):
            requested = list(action['keys'])
            found, missing = ({}, [])
            for line in output.splitlines():
                if '=' not in line:
                    continue
                k, _, v = line.partition('=')
                if v == '<<<MISSING>>>':
                    missing.append(k)
                else:
                    found[k] = v
            missing.extend((k for k in requested if k not in found and k not in missing))
            if found:
                lines = [f'{k}={found[k]}' for k in requested if k in found]
                self.output.append(f"""<br><span style="color:{T['TEXT_DIM']}">kubectl output:</span><br><span style="color:{T['TEXT_PRIMARY']}">{self._escape_html(chr(10).join(lines)).replace(chr(10), '<br>')}</span>""")
            if missing:
                missing_list = ', '.join(missing)
                note = f'Key not found: {missing_list} in resource. Check the key name and try again.'
                self._write_error(f'\n {note}' if not found else f'\n {note}')
            summary = ', '.join((f'{k}={found[k]}' for k in requested if k in found))
            if missing:
                summary = (summary + '; ' if summary else '') + 'missing: ' + ', '.join(missing)
            if found:
                self._remember_operation(action, command, 'success', summary)
                self._write_success('\n Kubernetes operation completed successfully.')
                self._speak(_spoken_done_phrase(action, success=True))
                self.operation_finished.emit()
            else:
                self._remember_operation(action, command, 'failed', summary)
                self._speak(_spoken_done_phrase(action, success=False))
            return
        combined_lower = (output + ' ' + error_text).lower()
        resource_missing = bool(re.search('error from server\\s*\\(\\s*notfound\\s*\\)', combined_lower))
        if action.get('action') == 'get' and action.get('key') and (not resource_missing) and (exit_code != 0 or (not error_text and (not output))):
            message = 'Key not found. in resource. Check the key name and try again.'
            self._remember_operation(action, command, 'failed', message)
            self._write_error(f'\n {message}')
            self._speak(_spoken_done_phrase(action, success=False))
            return
        if output:
            self.output.append(f"""<br><span style="color:{T['TEXT_DIM']}">kubectl output:</span><br><span style="color:{T['TEXT_PRIMARY']}">{self._escape_html(output).replace(chr(10), '<br>')}</span>""")
        if exit_code != 0 or error_text:
            combined = error_text or output or 'kubectl returned a non-zero exit code.'
            self._remember_operation(action, command, 'failed', combined)
            self._write_error(f'\n Kubernetes operation failed (exit code {exit_code}):\n{combined}')
            self._speak(_spoken_done_phrase(action, success=False))
            return
        self._remember_operation(action, command, 'success', output)
        self._write_success('\n Kubernetes operation completed successfully.')
        self._speak(_spoken_done_phrase(action, success=True))
        self.operation_finished.emit()

    def _on_operation_error(self, error: str):
        self._write_error('\n Kubernetes operation failed:\n' + str(error))
        action = None
        if self._operation_worker is not None:
            action = getattr(self._operation_worker, '_k8s_action', None)
            command = getattr(self._operation_worker, '_k8s_command', '')
            if action:
                action['_confirmed'] = bool(getattr(self._operation_worker, '_k8s_confirmed', False))
                self._remember_operation(action, command, 'failed', str(error))
        self._speak(_spoken_done_phrase(action, success=False) if action else 'Operation failed.')

    def _on_operation_finished(self):
        self._operation_worker = None
        self._set_busy(False)

    def _remember_operation(self, action: dict, command: str, status: str, output: str):
        row = {'timestamp': datetime.now().astimezone().isoformat(timespec='seconds'), 'status': status, 'action': action.get('action'), 'resource': action.get('resource'), 'name': action.get('name'), 'namespace': action.get('namespace', ''), 'context': action.get('context', ''), 'command': command}
        if action.get('action') == 'scale':
            row['replicas'] = action.get('replicas')
            if action.get('previous_replicas') is not None:
                row['previous_replicas'] = action.get('previous_replicas')
        if status == 'success':
            # An all-namespaces read leaves the action's namespace empty; keep
            # where the match was actually found so follow-ups like "delete
            # that pod" can resolve a concrete namespace.
            resolved = _parse_resolved_targets(action, output or '')
            if resolved:
                row['resolved'] = resolved
        persisted_output = output or ''
        if action.get('action') == 'get' and action.get('resource') == 'secret':
            persisted_output = '[REDACTED: secret values omitted from history/audit]'
        if persisted_output:
            row['output'] = persisted_output[-1000:]
        _append_audit(action, command, status, persisted_output, confirmed=bool(action.get('_confirmed', False)), risk=_risk_level(action, action.get('context', ''), is_protected_namespace(action.get('namespace', ''))))
        self._history.append(row)
        self._history = self._history[-MAX_HISTORY:]
        _save_history(self._history)
        self._update_history_label()

    def _show_audit_log(self):
        rows = _load_audit_log()
        dlg = QDialog(self)
        dlg.setWindowTitle('Kubernetes Audit Log')
        dlg.resize(900, 560)
        lay = QVBoxLayout(dlg)
        browser = QTextBrowser()
        browser.setFont(monospace_font(10))
        if not rows:
            browser.setPlainText('No Kubernetes operations have been audited yet.')
        else:
            lines = []
            for row in reversed(rows):
                lines.append(f"[{row.get('timestamp', '')}] {str(row.get('status', '')).upper()} risk={row.get('risk', 'low')}\ncontext:  {row.get('context', '—')}\nnamespace: {row.get('namespace') or 'all namespaces'}\noperation: {row.get('action', '')} {row.get('resource', '')}/{row.get('name', '')}\ncommand:  {row.get('command', '')}\nconfirmed: {row.get('confirmed', False)}\noutput:  {row.get('output', '').strip()}\n" + '-' * 88 + '\n')
            browser.setPlainText(''.join(lines))
        lay.addWidget(browser)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(dlg.reject)
        close.accepted.connect(dlg.accept)
        lay.addWidget(close)
        dlg.exec_()

    def closeEvent(self, event):
        if self._voice_worker is not None:
            try:
                self._voice_worker.stop()
            except Exception:
                pass
        if self._narrator is not None:
            try:
                self._narrator.stop()
            except Exception:
                pass
        if self._ai_worker is not None:
            try:
                self._ai_worker.quit()
            except Exception:
                pass
        if self._operation_worker is not None:
            try:
                self._operation_worker.quit()
            except Exception:
                pass
        super().closeEvent(event)
