import React, { createContext, useState, useEffect, useContext, useRef } from 'react';
import * as api from './api';

// Create authentication context
const AuthContext = createContext(null);

// Custom hook to use the auth context
export const useAuth = () => {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};

// Authentication provider component
export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [tokenRefreshTimer, setTokenRefreshTimer] = useState(null);
  const [refreshInProgress, setRefreshInProgress] = useState(false);
  const [isLoggingOut, setIsLoggingOut] = useState(false);
  const refreshAttempts = useRef(0);
  const MAX_REFRESH_ATTEMPTS = 3;
  const refreshPromise = useRef(null); // To store the promise during refresh

  // Function to check if token is expired or about to expire
  const isTokenExpiredOrExpiring = (expiresAt) => {
    if (!expiresAt) return true;
    
    try {
      // Check if within 1 minute of expiration (60 seconds) - Reduced buffer
      const currentTime = Math.floor(Date.now() / 1000);
      const bufferTime = 60; // 1 minute in seconds
      return currentTime + bufferTime >= expiresAt;
    } catch (err) {
      console.error('Error checking token expiration:', err);
      return true; // Assume expired on error
    }
  };

  // Centralized function to update authentication state
  const updateAuthState = (accessToken, expiresIn, userData) => {
    const tokenExpiry = Math.floor(Date.now() / 1000) + expiresIn;
    const authData = {
      accessToken,
      expiresIn,
      tokenExpiry,
      user: userData,
    };
    
    setUser(userData);
    api.setAuthToken(accessToken);
    localStorage.setItem('auth', JSON.stringify(authData));
    setupTokenRefresh(expiresIn, tokenExpiry);
    setError(null); // Clear previous errors on successful auth update
  };
  
  // Logout function
  const logout = async () => {
    setIsLoggingOut(true);
    console.log('Logging out user...');
    if (tokenRefreshTimer) {
      clearTimeout(tokenRefreshTimer);
      setTokenRefreshTimer(null);
    }
    setUser(null);
    setError(null);
    api.clearAuthToken();
    localStorage.removeItem('auth');
    refreshPromise.current = null; // Clear any pending refresh promise
    setRefreshInProgress(false); // Ensure refresh flag is reset
    refreshAttempts.current = 0; // Reset attempts on logout
    
    try {
      // Inform the backend about logout (optional, but good practice)
      await api.logout(); 
    } catch (err) {
      console.warn('Logout API call failed (might be expected if already logged out):', err);
    }
    
    // Redirect to login page if not already there
    if (typeof window !== 'undefined' && !window.location.pathname.includes('/login')) {
      // Add a query parameter to indicate session expiration if applicable
      const reason = error ? 'error' : 'manual_logout'; 
      // Using replace to avoid adding logout to history
      window.location.replace(`/login?logout_reason=${reason}`);
    }
  };

  // Unified and Robust Token Refresh Function
  const refreshToken = async () => {
    // If a refresh is already in progress, return the existing promise
    if (refreshInProgress && refreshPromise.current) {
      console.log('Refresh already in progress, returning existing promise.');
      return refreshPromise.current;
    }

    // Prevent multiple simultaneous refresh attempts
    if (refreshInProgress) {
      console.log('Refresh already in progress but promise missing, skipping.');
      // Avoid returning undefined, maybe return a rejected promise?
      return Promise.reject(new Error("Refresh conflict")); 
    }

    setRefreshInProgress(true);
    refreshAttempts.current = 0; // Reset attempts for a new refresh sequence
    console.log('Starting token refresh sequence.');

    const executeRefresh = async (attempt = 1) => {
      console.log(`Attempting token refresh (Attempt ${attempt}/${MAX_REFRESH_ATTEMPTS})`);
      try {
        const response = await api.refreshAuthToken();
        const { access_token, expires_in, user: userData } = response;
        
        console.log('Token successfully refreshed.');
        updateAuthState(access_token, expires_in, userData);
        setRefreshInProgress(false);
        refreshAttempts.current = 0; // Reset counter on success
        refreshPromise.current = null; // Clear the promise on success
        return true; // Indicate success
      } catch (err) {
        console.error(`Token refresh attempt ${attempt} failed:`, err);
        
        // Check if it's an auth error (e.g., invalid refresh token) or max attempts reached
        if (err.response?.status === 401 || attempt >= MAX_REFRESH_ATTEMPTS) {
          console.warn(`Refresh failed permanently (status ${err.response?.status}) or max attempts reached. Logging out.`);
          await logout(); // Use await here
          setRefreshInProgress(false); 
          refreshPromise.current = null; // Clear the promise on failure
          throw new Error("Token refresh failed permanently."); // Propagate error
        } else {
          // Schedule retry with exponential backoff
          const backoffTime = Math.pow(2, attempt) * 1000; // Exponential backoff (1s, 2s, 4s...)
          console.log(`Scheduling retry attempt ${attempt + 1} in ${backoffTime / 1000} seconds`);
          
          return new Promise(resolve => setTimeout(resolve, backoffTime))
            .then(() => executeRefresh(attempt + 1));
        }
      }
    };
    
    // Store the promise for the entire refresh sequence
    refreshPromise.current = executeRefresh();
    
    // Return the promise so callers can await the final result
    return refreshPromise.current.finally(() => {
        // Final cleanup, ensure flag is reset even if something unexpected happened
        setRefreshInProgress(false); 
        // Don't nullify the promise here if it's still being awaited elsewhere?
        // Let's reconsider - the promise is resolved/rejected, so it's fine to clear.
        refreshPromise.current = null; 
    });
  };


  // Wrapper function for initiating refresh, primarily used by initAuth
  // Returns the promise from refreshToken
  const attemptSilentRefresh = async () => {
    console.log("Attempting silent refresh via refreshToken function.");
    // Directly call the robust refreshToken function
    // No need for separate logic or attempts here anymore
    return refreshToken(); 
  };


  // Initialize auth state
  const initAuth = async () => {
    try {
      setLoading(true);
      setError(null); // Clear errors on init
      const authData = localStorage.getItem('auth');
      
      if (authData) {
        const parsedAuth = JSON.parse(authData);
        
        if (parsedAuth.user && parsedAuth.accessToken && parsedAuth.tokenExpiry) {
           // Check if token is expired or about to expire
          const isExpired = isTokenExpiredOrExpiring(parsedAuth.tokenExpiry);
          
          if (isExpired) {
            console.log('Stored token is expired or needs refresh, attempting silent refresh...');
            try {
              // Await the result of the robust refresh process
              await attemptSilentRefresh(); 
              console.log("Silent refresh successful during init.");
              // State is updated within refreshToken/updateAuthState
              // Fetch credits if user is now set
              if (user) fetchUserCredits(); 
            } catch (refreshError) {
               // refreshToken handles logout on permanent failure
              console.error("Silent refresh failed during init:", refreshError.message);
              // Ensure loading is false even if refresh fails and logout occurs
              setLoading(false);
              return; // Stop further initialization as user is logged out
            }
          } else {
            // Token is still valid
            console.log('Using valid stored token.');
            // Use updateAuthState to ensure consistency
            updateAuthState(parsedAuth.accessToken, parsedAuth.expiresIn, parsedAuth.user);
            
            // Fetch initial credit information
            fetchUserCredits();
          }
        } else {
           console.log("Stored auth data incomplete, clearing.");
           logout(); // Clean up incomplete data
        }
      } else {
         console.log("No stored auth data found.");
         // Ensure user is null if no auth data
         setUser(null); 
      }
    } catch (err) {
      console.error('Error initializing auth:', err);
      setError('Failed to initialize authentication state.');
      await logout(); // Attempt logout on initialization error
    } finally {
      setLoading(false);
    }
  };

  // Initialize auth when component mounts
  useEffect(() => {
    // Define async function inside useEffect or use IIFE
    const initialize = async () => {
      await initAuth();
    };
    initialize();
    
    // Listener for visibility changes (user returning to tab)
    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible' && user) {
        // Check token validity when user returns
        const authData = localStorage.getItem('auth');
        if (authData) {
          try {
            const parsedAuth = JSON.parse(authData);
            if (parsedAuth.tokenExpiry && isTokenExpiredOrExpiring(parsedAuth.tokenExpiry)) {
              console.log('Token expired or needs refresh while page was inactive, initiating refresh.');
              // Call refreshToken directly - no need to await here, let it run in background
              // refreshToken handles logout on failure internally.
              refreshToken().catch(err => {
                  console.error("Background refresh triggered by visibility change failed:", err.message);
                  // Logout is handled within refreshToken if needed
              });
            }
          } catch (e) {
              console.error("Error parsing auth data on visibility change:", e);
              logout(); // Logout if stored data is corrupt
          }
        } else if (user) {
            // If we have a user state but no localStorage, logout to sync state
            console.warn("User state exists but no auth data in localStorage. Logging out.");
            logout();
        }
      }
    };
    
    document.addEventListener('visibilitychange', handleVisibilityChange);
    
    // Listener for storage changes (sync across tabs)
    const handleStorageChange = (e) => {
      if (e.key === 'auth') {
         console.log("Auth data changed in another tab/window.");
        if (!e.newValue) {
          // Auth was cleared in another tab - logout here too
          console.log("Auth cleared elsewhere. Logging out this tab.");
          if (user) { // Only logout if currently logged in this tab
             logout();
          }
        } else {
           // Auth was updated in another tab - re-initialize this tab's state from storage
           console.log("Auth updated elsewhere. Re-initializing this tab.");
           // Re-run initAuth to ensure consistency and potentially fetch new user data/credits
           initAuth(); 
        }
      }
    };
    
    window.addEventListener('storage', handleStorageChange);
    
    // Cleanup on unmount
    return () => {
      if (tokenRefreshTimer) {
        clearTimeout(tokenRefreshTimer);
      }
      document.removeEventListener('visibilitychange', handleVisibilityChange);
      window.removeEventListener('storage', handleStorageChange);
    };
     // Rerun useEffect only if needed - dependencies are tricky here.
     // Since initAuth/logout modify state, they might cause loops if included.
     // Empty array means run once on mount. This seems correct for setup/cleanup.
  }, []); 

  // Set up token refresh timer
  const setupTokenRefresh = (expiresInSeconds, tokenExpiry) => {
    if (tokenRefreshTimer) {
      clearTimeout(tokenRefreshTimer);
    }

     // Calculate time until token is considered "expiring" based on our check
    const bufferTimeSeconds = 60; // Match the buffer in isTokenExpiredOrExpiring
    const nowSeconds = Math.floor(Date.now() / 1000);
    const timeUntilExpiryCheck = (tokenExpiry - bufferTimeSeconds - nowSeconds) * 1000;

    // Ensure refresh time is positive and has a minimum delay to avoid loops
    const safeRefreshTime = Math.max(timeUntilExpiryCheck, 10000); // At least 10 seconds

    console.log(`Scheduling next token refresh check in ${safeRefreshTime / 1000} seconds`);
    
    const timer = setTimeout(() => {
       console.log("Scheduled refresh timer triggered.");
       // Call refreshToken directly. It handles the logic internally.
       refreshToken().catch(err => {
         console.error("Scheduled refresh failed:", err.message);
         // Logout is handled within refreshToken if needed
       });
    }, safeRefreshTime);

    setTokenRefreshTimer(timer);
  };

  // Fetch user credits (unchanged, assuming it works)
  const fetchUserCredits = async () => {
    // No changes needed here based on the spec
    if (!user) return;
    
    try {
      const { credits } = await api.getUserCredits();
      
      // Only update if credits changed to prevent unnecessary renders
      if (user.credits !== credits) {
        setUser(prevUser => {
           const updatedUser = { ...prevUser, credits };
           // Also update localStorage to keep it in sync
           const authData = localStorage.getItem('auth');
           if (authData) {
              try {
                 const parsedAuth = JSON.parse(authData);
                 parsedAuth.user = updatedUser;
                 localStorage.setItem('auth', JSON.stringify(parsedAuth));
              } catch (e) {
                 console.error("Failed to update credits in localStorage:", e);
              }
           }
           return updatedUser;
        });
      }
    } catch (err) {
      console.error('Failed to fetch user credits:', err);
      // Decide if this error should cause logout or just be logged
      if (err.response?.status === 401) {
         console.warn("Unauthorized fetching credits, likely needs refresh/logout.");
         // Refresh might be triggered by interceptor, or we could trigger it here.
         // Let's rely on the interceptor or next scheduled refresh for now.
      }
    }
  };

  // Register function (unchanged)
  const register = async (email, password, fullName) => {
    try {
      setError(null);
      const response = await api.register(email, password, fullName);
      const { access_token, expires_in, user: userData } = response;
      updateAuthState(access_token, expires_in, userData);
      fetchUserCredits(); // Fetch credits after registration
      return userData;
    } catch (err) {
      console.error('Registration failed:', err);
      setError(err.message || 'Registration failed');
      throw err;
    }
  };

  // Login function (mostly unchanged, ensure updateAuthState is called)
  const login = async (email, password, rememberMe) => {
    try {
      setError(null);
      const response = await api.login(email, password, rememberMe);
      const { access_token, expires_in, user: userData } = response;
      updateAuthState(access_token, expires_in, userData);
      fetchUserCredits(); // Fetch credits after login
      return userData;
    } catch (err) {
      console.error('Login failed:', err);
      setError(err.message || 'Login failed');
      await logout(); // Logout fully on login failure
      throw err;
    }
  };

  // (logout function moved higher up for clarity)

  // Placeholder for migrateLearningPaths (unchanged)
  const migrateLearningPaths = async () => {
     // Existing implementation...
     console.warn("migrateLearningPaths not implemented yet.");
  };

  // Placeholder for checkPendingMigration (unchanged)
  const checkPendingMigration = () => {
     // Existing implementation...
     return false;
  };


  // Provide auth context value
  const authContextValue = {
    user,
    loading,
    error,
    isAuthenticated: !!user && !loading, // More accurate isAuthenticated check
    isLoggingOut,
    login,
    logout,
    register,
    fetchUserCredits, // Expose credit fetching if needed by components
    migrateLearningPaths,
    checkPendingMigration,
    // Don't expose internal functions like refreshToken directly if not needed
  };

  return (
    <AuthContext.Provider value={authContextValue}>
      {children}
    </AuthContext.Provider>
  );
}; 