// Account claims come only from the authenticated server session.
(function () {
  var css = document.createElement('style');
  css.textContent = `
.account-menu { position:relative; }
.account-menu summary { display:flex;align-items:center;gap:8px;list-style:none;border-radius:8px;padding:4px; }
.account-menu summary::-webkit-details-marker { display:none; }
.account-avatar { display:grid;place-items:center;width:32px;height:32px;border-radius:50%;background:var(--color-bg-row-selected);color:var(--color-brand-ink);font-size:11px;font-weight:600; }
.account-name { max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px; }
.account-panel { position:absolute;right:0;top:calc(100% + 12px);z-index:100;width:260px;max-width:calc(100vw - 32px);padding:16px;border:1px solid var(--color-border-control);border-radius:12px;background:var(--color-bg-surface);box-shadow:0 8px 24px #0002;font-size:13px;overflow-wrap:anywhere; }
.account-email,.account-method { color:var(--color-text-muted);font-size:12px;margin-top:4px; }
.account-panel form { border-top:1px solid var(--color-border);margin-top:12px;padding-top:12px; }
.account-panel button { width:100%;padding:8px;text-align:left; }
@media(max-width:599px){.workspace-header{flex-wrap:wrap}.header-controls{width:100%}.account-name{display:none}}
`;
  document.head.appendChild(css);
  function showAccount(profile) {
    var controls = document.querySelector('.header-controls');
    if (!controls || document.getElementById('account-menu')) return;
    var menu = document.createElement('details');
    menu.id = 'account-menu'; menu.className = 'account-menu';
    menu.innerHTML = '<summary aria-label="User account"><span class="account-avatar" aria-hidden="true"></span><span class="account-name"></span></summary>' +
      '<div class="account-panel"><strong class="account-full-name"></strong><div class="account-email"></div><div class="account-method"></div></div>';
    var name = profile.name || 'Operator';
    menu.querySelector('summary').setAttribute('aria-label', 'User account: ' + name);
    menu.querySelector('.account-avatar').textContent = name.split(/\s+/).map(function(p) {return p[0];}).slice(0,2).join('').toUpperCase();
    menu.querySelector('.account-name').textContent = name;
    menu.querySelector('.account-full-name').textContent = name;
    menu.querySelector('.account-email').textContent = profile.email || '';
    menu.querySelector('.account-method').textContent = profile.authentication === 'oidc' ? 'Signed in with SSO' : profile.authentication === 'token' ? 'Shared-token access' : 'Local access';
    if (profile.authentication === 'oidc') {
      var form = document.createElement('form'); form.method = 'post'; form.action = '/oidc/logout?redirect=1';
      var button = document.createElement('button'); button.type = 'submit'; button.textContent = 'Sign out';
      form.appendChild(button); menu.querySelector('.account-panel').appendChild(form);
    }
    controls.appendChild(menu);
    document.addEventListener('click', function(e) {if (!menu.contains(e.target)) menu.open = false;});
    document.addEventListener('keydown', function(e) {if (e.key === 'Escape' && menu.open) {menu.open = false;menu.querySelector('summary').focus();}});
  }
  function loadAccount() {
    fetch('/api/account', {cache:'no-store'}).then(function(r) {if (!r.ok) throw new Error('Account unavailable');return r.json();}).then(showAccount).catch(function() {});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', loadAccount); else loadAccount();
})();
