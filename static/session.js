// The pages' background requests (polling, folder levels, dialogs) get 401 with an X-Login header
// once the login has ended (signed out elsewhere, new password, expired): open the sign-in page
// and come back to this page afterwards. Loaded in <head>, before every other script.
(function () {
  const fetch = window.fetch;
  let leaving = false;
  window.fetch = function () {
    return fetch.apply(this, arguments).then((r) => {
      const login = r.status === 401 && r.headers.get("X-Login");
      if (login && !leaving) {
        leaving = true;
        location.assign(login + "?next=" + encodeURIComponent(location.pathname + location.search));
      }
      return r;
    });
  };
})();
