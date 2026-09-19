/* Human approval workflow. Server snapshots, never UI guesses, own decisions. */
(() => {
  let selectedRecord = null;
  let approvalSnapshot = null;
  let auditSnapshot = null;
  const rejectionReasons = [
    'PLAN_NOT_REVIEWED_YET', 'PREFER_ALTERNATIVE_PROFILE', 'DATA_ASSUMPTION_WRONG',
    'PRIORITY_CHANGED', 'OVERTIME_NOT_JUSTIFIED', 'OVERTIME_BUDGET_EXCEEDED',
    'DUE_DATE_COMMITMENT_UNACCEPTABLE', 'CUSTOMER_NOT_CONSULTED',
    'SECONDARY_SKILL_NOT_AUTHORISED', 'OTHER_SEE_COMMENT'
  ];
  const errorText = error => error.code ? error.code + '：' + error.message : error.message;
  const showError = error => { $('status').textContent = errorText(error); };
  const binding = () => {
    if (!selectedRecord) throw new Error('请先选择或读取计划');
    const content = selectedRecord.content;
    return {plan_id:content.plan_id, plan_version:content.plan_version,
      plan_digest:content.plan_digest};
  };

  async function requestApi(path, options = {}, role = 'planner') {
    const token = role === 'manager' ? $('managerToken').value.trim() : $('token').value.trim();
    if (!token) throw new Error(role === 'manager' ? '请填写经理 Token' : '请填写 Planner Token');
    const response = await fetch(path, {
      ...options,
      headers: {'Content-Type':'application/json', Authorization:'Bearer ' + token}
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.error || '请求失败 ' + response.status);
      error.code = data.error_code;
      throw error;
    }
    return data;
  }

  function updatePlanHeader() {
    if (!selectedRecord) return;
    const content = selectedRecord.content;
    const lifecycle = selectedRecord.lifecycle;
    $('state').textContent = lifecycle.status;
    $('version').textContent = content.plan_version;
    $('planid').textContent = content.plan_id;
    $('planLookup').value = content.plan_id;
    $('planVersionLookup').value = content.plan_version;
    $('approval').textContent = lifecycle.status === 'PUBLISHED'
      ? 'PUBLISHED' : (approvalSnapshot?.aggregate_status || lifecycle.status);
    if (current) {
      current.plan_id = content.plan_id;
      current.version = content.plan_version;
      current.content = content;
      current.lifecycle = lifecycle;
    }
    const index = stages.indexOf(lifecycle.status);
    $('timeline').replaceChildren(...stages.map((label, position) => {
      const node = document.createElement('div');
      node.className = 'step ' + (index >= 0 && position <= index ? 'done' : '');
      node.textContent = label;
      return node;
    }));
  }

  function appendText(parent, tag, value, className) {
    const node = document.createElement(tag);
    node.textContent = value;
    if (className) node.className = className;
    parent.append(node);
    return node;
  }

  function renderApprovals() {
    const root = $('approvals');
    root.replaceChildren();
    if (!selectedRecord) {
      root.textContent = '请选择计划';
      return;
    }
    if (!approvalSnapshot) {
      root.textContent = selectedRecord.lifecycle.approval_set_id
        ? '审批集合已绑定；点击“刷新审批状态”读取服务端状态。'
        : '尚未发起审批。发起一次即可创建全部所需审批项。';
      return;
    }
    appendText(root, 'strong', '集合 ' + approvalSnapshot.approval_set_id + ' · ' +
      approvalSnapshot.aggregate_status);
    for (const item of approvalSnapshot.approvals) {
      const row = document.createElement('div');
      row.className = 'approval-row';
      appendText(row, 'strong', item.action + ' · ' + item.status);
      appendText(row, 'div', '审批角色：' + item.approver_role + ' · 到期：' + item.expires_at,
        'muted');
      appendText(row, 'div', '请求：' + item.approval_request_id, 'muted');
      const impact = item.impact_summary || {};
      appendText(row, 'div', '影响订单：' + (impact.affected_order_ids || []).join(', ') +
        ' · 变更工序：' + (impact.changed_operation_count ?? 0) +
        ' · 原因：' + (impact.reason_codes || []).join(', '), 'muted');
      if (item.status === 'PENDING' && approvalSnapshot.aggregate_status === 'PENDING') {
        const controls = document.createElement('div');
        controls.className = 'actions';
        const approve = appendText(controls, 'button', '批准', 'btn');
        approve.type = 'button';
        const reject = appendText(controls, 'button', '拒绝', 'btn danger');
        reject.type = 'button';
        const reason = document.createElement('select');
        reason.setAttribute('aria-label', '拒绝原因');
        reason.replaceChildren(...rejectionReasons.map(code => new Option(code, code)));
        const comment = document.createElement('input');
        comment.placeholder = '拒绝说明（可选）';
        comment.setAttribute('aria-label', '拒绝说明');
        controls.append(reason, comment);
        approve.onclick = () => decide(item, 'APPROVED');
        reject.onclick = () => decide(item, 'REJECTED', reason.value, comment.value);
        row.append(controls);
      }
      root.append(row);
    }
    updatePlanHeader();
  }

  async function refreshApproval() {
    const setId = selectedRecord?.lifecycle?.approval_set_id;
    if (!setId) { approvalSnapshot = null; renderApprovals(); return; }
    const query = new URLSearchParams({approval_set_id:setId, ...binding()});
    approvalSnapshot = await requestApi('/approval/status?' + query);
    renderApprovals();
  }

  async function refreshAudit() {
    auditSnapshot = await requestApi('/audit/status');
    updateTrace();
  }

  function updateTrace() {
    const content = selectedRecord?.content;
    const lines = [];
    if (content) {
      lines.push('计划：' + content.plan_id + ' · 版本：' + content.plan_version);
      lines.push('摘要 SHA-256：' + content.plan_digest);
      lines.push('求解器：' + content.engine.solver + ' · ' + content.engine.solver_status);
    }
    if (agentTrace.length) lines.push('Agent 步骤：\n' + formatAgentTrace(agentTrace));
    if (auditSnapshot) {
      lines.push('审计链：' + (auditSnapshot.verified ? '已验证' : '验证失败') +
        ' · ' + auditSnapshot.entry_count + ' 条');
      lines.push('链头：' + auditSnapshot.head_hash);
      for (const row of auditSnapshot.recent) {
        lines.push(row.audit_log_id + ' · ' + row.event + ' · ' +
          (row.plan_id || '无计划') + ' · ' + row.event_hash);
      }
    }
    $('trace').textContent = lines.join('\n') || '尚无计划或审计记录';
  }

  function loadedCandidate(content) {
    const start = /^Planning horizon starts at (.+)\.$/.exec(content.assumptions?.[0] || '');
    const stamp = start ? Date.parse(start[1]) : 0;
    return {profile:content.profile, kpis:content.kpis, operations:content.operations.map(row => ({
      order_id:row.order_id, operation_no:row.operation_no, machine_id:row.machine_id,
      worker_id:row.worker_id, start:(Date.parse(row.start_time)-stamp)/60000,
      end:(Date.parse(row.end_time)-stamp)/60000,
    })), unscheduled_operations:content.unscheduled_operations, violations:[]};
  }

  async function selectProfile() {
    const profile = $('candidate').value;
    const option = (current?.plan_options || []).find(row => row.profile === profile);
    if (!option) return;
    const query = new URLSearchParams({plan_id:option.plan_id,
      version:String(option.plan_version)});
    selectedRecord = await requestApi('/plans?' + query);
    approvalSnapshot = null;
    updatePlanHeader();
    renderApprovals();
    if (selectedRecord.lifecycle.approval_set_id) await refreshApproval();
    await refreshAudit();
  }

  const oldRender = render;
  render = function () {
    oldRender();
    if (current?.content && current?.lifecycle) {
      selectedRecord = {content:current.content, lifecycle:current.lifecycle};
      $('candidate').value = current.content.profile;
      draw();
    }
    approvalSnapshot = null;
    updatePlanHeader();
    renderApprovals();
    $('candidate').onchange = () => {
      draw();
      selectProfile().catch(showError);
    };
    refreshAudit().catch(showError);
  };

  $('load').onclick = async () => {
    try {
      const id = $('planLookup').value.trim();
      if (!id) throw new Error('请填写计划 ID');
      const query = new URLSearchParams({plan_id:id});
      const version = $('planVersionLookup').value.trim();
      if (version) query.set('version', version);
      const record = await requestApi('/plans?' + query);
      current = {plan_id:record.content.plan_id, version:record.content.plan_version,
        state:record.lifecycle.status, content:record.content, lifecycle:record.lifecycle,
        candidates:[loadedCandidate(record.content)], plan_options:[{
          plan_id:record.content.plan_id, plan_version:record.content.plan_version,
          profile:record.content.profile,
        }]};
      render();
      await refreshApproval();
      $('status').textContent = '已读取服务端保存的计划与生命周期。';
    } catch (error) { showError(error); }
  };

  $('request').onclick = async () => {
    try {
      approvalSnapshot = await requestApi('/approval/request', {method:'POST',
        body:JSON.stringify({...binding(), action:'publish_plan'})});
      selectedRecord.lifecycle.approval_set_id = approvalSnapshot.approval_set_id;
      renderApprovals();
      await refreshAudit();
      $('status').textContent = '已发起全部服务端所需审批。';
    } catch (error) { showError(error); }
  };

  async function decide(item, decision, reason, comment) {
    try {
      const role = item.approver_role === 'Production Manager' ? 'manager' : 'planner';
      const body = {request_id:item.approval_request_id, decision};
      if (decision === 'REJECTED') {
        body.decision_reason = reason;
        if (comment.trim()) body.decision_comment = comment.trim();
      }
      approvalSnapshot = await requestApi('/approval/decide', {method:'POST',
        body:JSON.stringify(body)}, role);
      renderApprovals();
      await refreshAudit();
      $('status').textContent = decision === 'APPROVED' ? '审批决定已保存。' : '拒绝决定已保存；该计划不能发布。';
    } catch (error) { showError(error); }
  }

  $('refreshApproval').onclick = () => refreshApproval().catch(showError);
  $('refreshAudit').onclick = () => refreshAudit().catch(showError);
  $('publish').onclick = async () => {
    try {
      const b = binding();
      const setId = selectedRecord.lifecycle.approval_set_id;
      if (!setId) throw new Error('请先发起审批');
      await refreshApproval();
      const result = await requestApi('/publish', {method:'POST', body:JSON.stringify({
        plan_id:b.plan_id, expected_plan_version:b.plan_version,
        plan_digest:b.plan_digest, approval_set_id:setId,
        idempotency_key:'planpilot-web-publish-' + b.plan_id + '-' + b.plan_version,
      })});
      selectedRecord.lifecycle = result;
      updatePlanHeader();
      await refreshAudit();
      $('status').textContent = '计划已发布，版本与摘要已重新核对。';
    } catch (error) { showError(error); }
  };
})();
