/**
 * Theme Manager for ForrixGuard
 * Handles Light/Dark mode toggling, persistence, and UI injection.
 */

(function () {
    const THEME_KEY = 'forrixguard_theme';
    const THEME_DARK = 'dark';
    const THEME_LIGHT = 'light';

    // 1. Initial Load: Check persistence
    function applySavedTheme() {
        const savedTheme = localStorage.getItem(THEME_KEY);
        // Default to DARK if no theme is saved, or if saved is explicitly 'dark'
        if (!savedTheme || savedTheme === THEME_DARK) {
            document.body.classList.add('theme-dark');
        } else {
            document.body.classList.remove('theme-dark');
        }
    }

    // 2. Toggle Logic
    window.toggleTheme = function () { // Expose globally
        const isDark = document.body.classList.toggle('theme-dark');
        localStorage.setItem(THEME_KEY, isDark ? THEME_DARK : THEME_LIGHT);
        updateButtonIcon(isDark);

        // Dispatch custom event for charts and other reactive components
        window.dispatchEvent(new CustomEvent('themeChanged', { detail: { isDark } }));
    }

    // 3. UI Update (Icon)
    function updateButtonIcon(isDark) {
        // Handle Main Nav Icon
        const btnIcon = document.getElementById('theme-toggle-icon');
        if (btnIcon) {
            btnIcon.className = isDark ? 'fa-solid fa-moon' : 'fa-solid fa-sun';
            btnIcon.style.color = isDark ? '#50BBA9' : '#F59E0B';
        }

        // Handle Login Page Icon
        const loginIcon = document.getElementById('login-theme-icon');
        if (loginIcon) {
            loginIcon.className = isDark ? 'fa-solid fa-moon' : 'fa-solid fa-sun';
            // Adjust color for login page context if needed
            loginIcon.style.color = isDark ? '#50BBA9' : '#F59E0B';
        }
    }

    // 4. Inject Button into Navbar
    function injectThemeButton() {
        // Wait for DOM
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', injectThemeButton);
            return;
        }

        // SCENARIO 1: Main Dashboard Navbar
        const navBar = document.querySelector('.nav-bar');
        if (navBar && !document.getElementById('theme-toggle-btn')) {
            const btn = document.createElement('button');
            btn.id = 'theme-toggle-btn';
            btn.className = 'nav-btn';
            btn.title = 'Toggle Theme';
            btn.onclick = window.toggleTheme;
            btn.style.padding = '0.6rem';
            btn.style.display = 'flex';
            btn.style.alignItems = 'center';
            btn.style.justifyContent = 'center';
            btn.style.width = '40px';
            btn.style.marginRight = '0.5rem';

            const isDark = document.body.classList.contains('theme-dark');
            const iconClass = isDark ? 'fa-solid fa-moon' : 'fa-solid fa-sun';
            const iconColor = isDark ? '#50BBA9' : '#F59E0B';

            btn.innerHTML = `<i id="theme-toggle-icon" class="${iconClass}" style="color: ${iconColor};"></i>`;

            const logoutBtn = document.getElementById('logout-btn');
            if (logoutBtn) {
                navBar.insertBefore(btn, logoutBtn);
            } else {
                navBar.appendChild(btn);
            }
        }

        // SCENARIO 2: Login Page (Manual Injection Helper)
        // Check if we are on login page and update the icon state initially
        if (document.getElementById('login-theme-btn')) {
            const isDark = document.body.classList.contains('theme-dark');
            updateButtonIcon(isDark);
        }
    }

    // Run on load
    applySavedTheme();
    injectThemeButton();

})();
