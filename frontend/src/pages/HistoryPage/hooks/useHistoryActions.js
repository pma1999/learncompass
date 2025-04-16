import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import * as api from '../../../services/api';

/**
 * Custom hook for managing history entry actions
 * @param {Function} showNotification - Function to show notifications
 * @param {Function} refreshEntries - Function to refresh entries after actions
 * @returns {Object} Actions and related states for history management
 */
const useHistoryActions = (showNotification, refreshEntries) => {
  const navigate = useNavigate();
  
  /**
   * View a learning path by navigating to its detail page
   * @param {string} pathId - path_id of the entry to view
   */
  const handleViewLearningPath = (pathId) => {
    try {
      navigate(`/history/${pathId}`);
    } catch (error) {
      console.error('Error navigating to learning path:', error);
      showNotification('Error loading learning path: ' + (error.message || 'Unknown error'), 'error');
    }
  };
  
  /**
   * Delete a learning path
   * @param {string} pathId - path_id of the entry to delete
   */
  const handleDeleteLearningPath = async (pathId) => {
    try {
      await api.deleteHistoryEntry(pathId);
      showNotification('Learning path deleted successfully', 'success');
      refreshEntries();
    } catch (error) {
      showNotification('Error deleting learning path: ' + (error.message || 'Unknown error'), 'error');
    }
  };
  
  /**
   * Toggle favorite status of a learning path
   * @param {string} pathId - path_id of the entry to update
   * @param {boolean} favoriteStatus - New favorite status
   */
  const handleToggleFavorite = async (pathId, favoriteStatus) => {
    try {
      await api.updateHistoryEntry(pathId, { favorite: favoriteStatus });
      showNotification(
        favoriteStatus ? 'Added to favorites' : 'Removed from favorites',
        'success'
      );
      refreshEntries();
    } catch (error) {
      showNotification('Error updating favorite status: ' + (error.message || 'Unknown error'), 'error');
    }
  };
  
  /**
   * Update tags for a learning path
   * @param {string} pathId - path_id of the entry to update
   * @param {Array<string>} tags - New tags array
   */
  const handleUpdateTags = async (pathId, tags) => {
    try {
      await api.updateHistoryEntry(pathId, { tags });
      showNotification('Tags updated successfully', 'success');
      refreshEntries();
    } catch (error) {
      showNotification('Error updating tags: ' + (error.message || 'Unknown error'), 'error');
    }
  };
  
  /**
   * Download a single learning path as PDF file
   * @param {string} pathId - path_id of the entry to download as PDF
   */
  const handleDownloadPDF = async (pathId) => {
    try {
      // Show loading notification
      showNotification('Generating PDF...', 'info');
      
      // Get the learning path name for better feedback
      const response = await api.getHistoryEntry(pathId);
      const topic = response.entry.topic;
      
      // Download the PDF using the API function
      const pdfBlob = await api.downloadLearningPathPDF(pathId);
      
      // Create a download link
      const url = URL.createObjectURL(pdfBlob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `learning_path_${topic.replace(/\s+/g, '_')}.pdf`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      
      showNotification('PDF downloaded successfully', 'success');
    } catch (error) {
      console.error('Error downloading PDF:', error);
      showNotification('Failed to download PDF: ' + (error.message || 'Unknown error'), 'error');
    }
  };

  return {
    // Actions
    handleViewLearningPath,
    handleDeleteLearningPath,
    handleToggleFavorite,
    handleUpdateTags,
    handleDownloadPDF
  };
};

export default useHistoryActions; 