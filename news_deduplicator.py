#!/usr/bin/env python3
"""
News Deduplicator Module
Advanced content similarity detection to prevent duplicate news from inflating scores
Uses TF-IDF cosine similarity to detect similar articles across sources
"""

import re
import logging
from typing import List, Set, Tuple
from collections import Counter
import math

logger = logging.getLogger(__name__)


class NewsDeduplicator:
    """Detects and removes duplicate news articles using content similarity"""

    def __init__(self, similarity_threshold: float = 0.75):
        """
        Args:
            similarity_threshold: Articles with similarity >= threshold are considered duplicates (0.0-1.0)
        """
        self.similarity_threshold = similarity_threshold
        self.stopwords = self._get_stopwords()

    def _get_stopwords(self) -> Set[str]:
        """Common English stopwords to exclude from similarity calculation"""
        return {
            'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
            'of', 'with', 'by', 'from', 'up', 'about', 'into', 'through', 'during',
            'is', 'are', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had',
            'do', 'does', 'did', 'will', 'would', 'should', 'could', 'may', 'might',
            'can', 'this', 'that', 'these', 'those', 'i', 'you', 'he', 'she', 'it',
            'we', 'they', 'them', 'their', 'what', 'which', 'who', 'when', 'where',
            'why', 'how', 'all', 'each', 'every', 'both', 'few', 'more', 'most',
            'some', 'such', 'no', 'nor', 'not', 'only', 'same', 'so', 'than', 'too',
            'very', 's', 't', 'just', 'don', 'now'
        }

    def _tokenize(self, text: str) -> List[str]:
        """Convert text to lowercase tokens, removing stopwords and punctuation"""
        # Lowercase and split by non-alphanumeric characters
        tokens = re.findall(r'\b[a-z]+\b', text.lower())
        
        # Remove stopwords and very short tokens
        tokens = [t for t in tokens if t not in self.stopwords and len(t) > 2]
        
        return tokens

    def _compute_tf(self, tokens: List[str]) -> dict:
        """Compute term frequency (TF) for tokens"""
        token_count = Counter(tokens)
        total_tokens = len(tokens)
        
        if total_tokens == 0:
            return {}
        
        # Normalize by total tokens
        return {token: count / total_tokens for token, count in token_count.items()}

    def _cosine_similarity(self, tf1: dict, tf2: dict) -> float:
        """Calculate cosine similarity between two TF vectors"""
        # Get all unique tokens
        all_tokens = set(tf1.keys()) | set(tf2.keys())
        
        if not all_tokens:
            return 0.0
        
        # Calculate dot product and magnitudes
        dot_product = sum(tf1.get(token, 0) * tf2.get(token, 0) for token in all_tokens)
        magnitude1 = math.sqrt(sum(v ** 2 for v in tf1.values()))
        magnitude2 = math.sqrt(sum(v ** 2 for v in tf2.values()))
        
        if magnitude1 == 0 or magnitude2 == 0:
            return 0.0
        
        return dot_product / (magnitude1 * magnitude2)

    def calculate_similarity(self, article1, article2) -> float:
        """
        Calculate similarity between two NewsArticle objects
        
        Returns:
            Similarity score from 0.0 (completely different) to 1.0 (identical)
        """
        # Combine title and description for comparison
        text1 = f"{article1.title} {article1.description}"
        text2 = f"{article2.title} {article2.description}"
        
        # Tokenize
        tokens1 = self._tokenize(text1)
        tokens2 = self._tokenize(text2)
        
        # Quick check: if very few common tokens, skip expensive calculation
        common_tokens = set(tokens1) & set(tokens2)
        if len(common_tokens) < 3:
            return 0.0
        
        # Compute TF vectors
        tf1 = self._compute_tf(tokens1)
        tf2 = self._compute_tf(tokens2)
        
        # Calculate cosine similarity
        return self._cosine_similarity(tf1, tf2)

    def deduplicate(self, articles: List, keep_strategy: str = 'highest_relevance') -> Tuple[List, int]:
        """
        Remove duplicate articles based on content similarity
        
        Args:
            articles: List of NewsArticle objects
            keep_strategy: Which article to keep when duplicates found
                          'highest_relevance' - Keep article with highest relevance score
                          'first' - Keep first article encountered
                          'most_recent' - Keep most recently published
        
        Returns:
            Tuple of (deduplicated_articles, num_duplicates_removed)
        """
        if not articles:
            return [], 0
        
        # Track which articles to keep
        unique_articles = []
        duplicate_groups = []  # For logging
        processed_indices = set()
        
        for i, article1 in enumerate(articles):
            if i in processed_indices:
                continue
            
            # Find all duplicates of this article
            duplicate_group = [article1]
            duplicate_indices = [i]
            
            for j, article2 in enumerate(articles[i+1:], start=i+1):
                if j in processed_indices:
                    continue
                
                similarity = self.calculate_similarity(article1, article2)
                
                if similarity >= self.similarity_threshold:
                    duplicate_group.append(article2)
                    duplicate_indices.append(j)
            
            # Mark all as processed
            processed_indices.update(duplicate_indices)
            
            # Choose which article to keep based on strategy
            if keep_strategy == 'highest_relevance':
                keeper = max(duplicate_group, key=lambda a: a.relevance_score)
            elif keep_strategy == 'most_recent':
                keeper = max(duplicate_group, key=lambda a: a.published_at)
            else:  # 'first'
                keeper = duplicate_group[0]
            
            unique_articles.append(keeper)
            
            # Log duplicate groups (only if duplicates found)
            if len(duplicate_group) > 1:
                duplicate_groups.append(duplicate_group)
        
        # Log results
        num_duplicates = len(articles) - len(unique_articles)
        if num_duplicates > 0:
            logger.info(f"Deduplication: {len(articles)} articles → {len(unique_articles)} unique ({num_duplicates} duplicates removed)")
            
            # Log details of duplicate groups
            for group in duplicate_groups:
                sources = [a.source for a in group]
                logger.debug(f"  Duplicate group ({len(group)}): {group[0].title[:60]}... from {sources}")
        
        return unique_articles, num_duplicates

    def find_duplicates(self, articles: List) -> List[List]:
        """
        Find all duplicate groups without removing them
        
        Returns:
            List of duplicate groups, where each group is a list of similar articles
        """
        duplicate_groups = []
        processed_indices = set()
        
        for i, article1 in enumerate(articles):
            if i in processed_indices:
                continue
            
            duplicate_group = [article1]
            duplicate_indices = [i]
            
            for j, article2 in enumerate(articles[i+1:], start=i+1):
                if j in processed_indices:
                    continue
                
                similarity = self.calculate_similarity(article1, article2)
                
                if similarity >= self.similarity_threshold:
                    duplicate_group.append(article2)
                    duplicate_indices.append(j)
            
            processed_indices.update(duplicate_indices)
            
            # Only add if there are actual duplicates
            if len(duplicate_group) > 1:
                duplicate_groups.append(duplicate_group)
        
        return duplicate_groups


# Standalone test
if __name__ == '__main__':
    from news_aggregator import NewsArticle
    from datetime import datetime
    
    # Create test articles
    articles = [
        NewsArticle(
            title="Federal Reserve raises interest rates to combat inflation",
            description="The Federal Reserve announced a rate hike today amid concerns about rising inflation",
            source="Reuters",
            published_at=datetime.now(),
            url="https://reuters.com/1",
            relevance_score=95.0
        ),
        NewsArticle(
            title="Fed increases rates in fight against inflation",
            description="In a move to combat inflation, the Federal Reserve raised interest rates this morning",
            source="Bloomberg",
            published_at=datetime.now(),
            url="https://bloomberg.com/2",
            relevance_score=90.0
        ),
        NewsArticle(
            title="Tesla stock surges on earnings beat",
            description="Tesla shares jumped 10% after reporting better than expected quarterly earnings",
            source="CNBC",
            published_at=datetime.now(),
            url="https://cnbc.com/3",
            relevance_score=85.0
        ),
        NewsArticle(
            title="Fed rate hike announced to tackle inflation crisis",
            description="The central bank raised rates by 50 basis points to address inflationary pressures",
            source="WSJ",
            published_at=datetime.now(),
            url="https://wsj.com/4",
            relevance_score=92.0
        ),
    ]
    
    print("Testing News Deduplicator...")
    print(f"\nInput: {len(articles)} articles\n")
    
    deduplicator = NewsDeduplicator(similarity_threshold=0.75)
    
    # Show similarity matrix
    print("Similarity Matrix:")
    print("-" * 60)
    for i, a1 in enumerate(articles):
        for j, a2 in enumerate(articles):
            if i < j:
                sim = deduplicator.calculate_similarity(a1, a2)
                print(f"[{i}] vs [{j}]: {sim:.2f} - {a1.title[:40]}... vs {a2.title[:40]}...")
    
    print("\n")
    
    # Deduplicate
    unique, num_removed = deduplicator.deduplicate(articles, keep_strategy='highest_relevance')
    
    print(f"\nOutput: {len(unique)} unique articles ({num_removed} duplicates removed)\n")
    for i, article in enumerate(unique, 1):
        print(f"{i}. [{article.source}] {article.title}")
        print(f"   Relevance: {article.relevance_score:.1f}/100")
        print()
