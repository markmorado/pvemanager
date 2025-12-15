/**
 * Theme Manager - Handles light/dark mode switching and sidebar toggle
 */

(function() {
    'use strict';

    const THEME_KEY = 'panel-theme';
    const SIDEBAR_KEY = 'panel-sidebar';
    const DARK_THEME = 'dark';
    const LIGHT_THEME = 'light';

    // Get saved theme or system preference
    function getPreferredTheme() {
        const savedTheme = localStorage.getItem(THEME_KEY);
        if (savedTheme) {
            return savedTheme;
        }
        
        // Check system preference
        if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
            return LIGHT_THEME;
        }
        
        return DARK_THEME;
    }

    // Apply theme to document
    function applyTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        localStorage.setItem(THEME_KEY, theme);
        
        // Update toggle button icon
        updateToggleIcon(theme);
    }

    // Update toggle button icon
    function updateToggleIcon(theme) {
        const toggle = document.getElementById('themeToggle');
        if (!toggle) return;
        
        const sunIcon = toggle.querySelector('.icon-sun');
        const moonIcon = toggle.querySelector('.icon-moon');
        
        if (sunIcon && moonIcon) {
            if (theme === DARK_THEME) {
                sunIcon.style.display = 'block';
                moonIcon.style.display = 'none';
            } else {
                sunIcon.style.display = 'none';
                moonIcon.style.display = 'block';
            }
        }
    }

    // Toggle theme
    function toggleTheme() {
        const currentTheme = document.documentElement.getAttribute('data-theme') || DARK_THEME;
        const newTheme = currentTheme === DARK_THEME ? LIGHT_THEME : DARK_THEME;
        applyTheme(newTheme);
    }

    // Toggle sidebar
    function toggleSidebar() {
        const sidebar = document.getElementById('sidebar');
        const overlay = document.getElementById('sidebarOverlay');
        
        if (sidebar) {
            sidebar.classList.toggle('open');
            
            if (overlay) {
                overlay.classList.toggle('active', sidebar.classList.contains('open'));
            }
        }
    }

    // Close sidebar on escape key
    function handleKeydown(e) {
        if (e.key === 'Escape') {
            const sidebar = document.getElementById('sidebar');
            const overlay = document.getElementById('sidebarOverlay');
            
            if (sidebar && sidebar.classList.contains('open')) {
                sidebar.classList.remove('open');
                if (overlay) {
                    overlay.classList.remove('active');
                }
            }
        }
    }

    // Initialize on DOM load
    function init() {
        // Apply saved or preferred theme immediately
        const theme = getPreferredTheme();
        applyTheme(theme);

        // Set up toggle button
        const toggle = document.getElementById('themeToggle');
        if (toggle) {
            toggle.addEventListener('click', toggleTheme);
        }

        // Listen for escape key
        document.addEventListener('keydown', handleKeydown);

        // Listen for system theme changes
        if (window.matchMedia) {
            window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
                // Only auto-switch if user hasn't manually selected a theme
                if (!localStorage.getItem(THEME_KEY)) {
                    applyTheme(e.matches ? DARK_THEME : LIGHT_THEME);
                }
            });
        }

        // Close sidebar when clicking on a link (mobile)
        const sidebarLinks = document.querySelectorAll('.sidebar-nav a');
        sidebarLinks.forEach(link => {
            link.addEventListener('click', () => {
                const sidebar = document.getElementById('sidebar');
                const overlay = document.getElementById('sidebarOverlay');
                
                if (window.innerWidth <= 768 && sidebar) {
                    sidebar.classList.remove('open');
                    if (overlay) {
                        overlay.classList.remove('active');
                    }
                }
            });
        });
    }

    // Apply theme before DOM is ready to prevent flash
    const savedTheme = getPreferredTheme();
    document.documentElement.setAttribute('data-theme', savedTheme);

    // Initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // Expose functions globally
    window.toggleTheme = toggleTheme;
    window.toggleSidebar = toggleSidebar;
})();
